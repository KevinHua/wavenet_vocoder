"""Trainining script for WaveNet vocoder

usage: train.py [options]

options:
    --data-root=<dir>            Directory contains preprocessed features.
    --checkpoint-dir=<dir>       Directory where to save model checkpoints [default: checkpoints].
    --hparams=<parmas>           Hyper parameters [default: ].
    --checkpoint=<path>          Restore model from checkpoint path if given.
    --restore-parts=<path>       Restore part of the model.
    --log-event-path=<name>      Log event path.
    --reset-optimizer            Reset optimizer.
    --speaker-id=<N>             Use specific speaker of data in case for multi-speaker datasets.
    -h, --help                   Show this help message and exit
"""
from docopt import docopt

import sys
from os.path import dirname, join
from tqdm import tqdm, trange
from datetime import datetime

from wavenet_vocoder import builder
import lrschedule

import torch
from torch.utils import data as data_utils
from torch.autograd import Variable
from torch import nn
from torch.nn import functional as F
from torch import optim
import torch.backends.cudnn as cudnn
from torch.utils import data as data_utils
from torch.utils.data.sampler import Sampler
import numpy as np

from nnmnkwii import preprocessing as P
from nnmnkwii.datasets import FileSourceDataset, FileDataSource

from os.path import join, expanduser
import random
import librosa.display
from matplotlib import pyplot as plt
import sys
import os

from sklearn.model_selection import train_test_split
from keras.utils import np_utils
from tensorboardX import SummaryWriter
from matplotlib import cm
from warnings import warn

import audio
from hparams import hparams, hparams_debug_string

fs = hparams.sample_rate

global_step = 0
global_test_step = 0
global_epoch = 0
use_cuda = torch.cuda.is_available()
if use_cuda:
    cudnn.benchmark = False


def _pad(seq, max_len, constant_values=0):
    return np.pad(seq, (0, max_len - len(seq)),
                  mode='constant', constant_values=constant_values)


def _pad_2d(x, max_len, b_pad=0):
    x = np.pad(x, [(b_pad, max_len - len(x) - b_pad), (0, 0)],
               mode="constant", constant_values=0)
    return x


class _NPYDataSource(FileDataSource):
    def __init__(self, data_root, col, speaker_id=None,
                 train=True, test_size=0.05, test_num_samples=None, random_state=1234):
        self.data_root = data_root
        self.col = col
        self.lengths = []
        self.speaker_id = speaker_id
        self.multi_speaker = False
        self.speaker_ids = None
        self.train = train
        self.test_size = test_size
        self.test_num_samples = test_num_samples
        self.random_state = random_state

    def interest_indices(self, paths):
        indices = np.arange(len(paths))
        if self.test_size is None:
            test_size = self.test_num_samples / len(paths)
        else:
            test_size = self.test_size
        train_indices, test_indices = train_test_split(
            indices, test_size=test_size, random_state=self.random_state)
        return train_indices if self.train else test_indices

    def collect_files(self):
        meta = join(self.data_root, "train.txt")
        with open(meta, "rb") as f:
            lines = f.readlines()
        l = lines[0].decode("utf-8").split("|")
        assert len(l) == 4 or len(l) == 5
        self.multi_speaker = len(l) == 5
        self.lengths = list(
            map(lambda l: int(l.decode("utf-8").split("|")[2]), lines))

        paths = list(map(lambda l: l.decode("utf-8").split("|")[self.col], lines))
        paths = list(map(lambda f: join(self.data_root, f), paths))

        if self.multi_speaker:
            speaker_ids = list(map(lambda l: int(l.decode("utf-8").split("|")[-1]), lines))
            self.speaker_ids = speaker_ids
            if self.speaker_id is not None:
                # Filter by speaker_id
                # using multi-speaker dataset as a single speaker dataset
                indices = np.array(speaker_ids) == self.speaker_id
                paths = list(np.array(paths)[indices])
                self.lengths = list(np.array(self.lengths)[indices])

                # Filter by train/tset
                indices = self.interest_indices(paths)
                paths = list(np.array(paths)[indices])
                self.lengths = list(np.array(self.lengths)[indices])

                # aha, need to cast numpy.int64 to int
                self.lengths = list(map(int, self.lengths))
                self.multi_speaker = False

                return paths

        # Filter by train/test
        indices = self.interest_indices(paths)
        paths = list(np.array(paths)[indices])
        self.lengths = list(np.array(self.lengths)[indices])
        self.lengths = list(map(int, self.lengths))

        if self.multi_speaker:
            self.speaker_ids = list(np.array(self.speaker_ids)[indices])
            self.speaker_ids = list(map(int, self.speaker_ids))
            assert len(paths) == len(self.speaker_ids)

        return paths

    def collect_features(self, path):
        return np.load(path)


class RawAudioDataSource(_NPYDataSource):
    def __init__(self, data_root, **kwargs):
        super(RawAudioDataSource, self).__init__(data_root, 0, **kwargs)


class MelSpecDataSource(_NPYDataSource):
    def __init__(self, data_root, **kwargs):
        super(MelSpecDataSource, self).__init__(data_root, 1, **kwargs)


class PartialyRandomizedSimilarTimeLengthSampler(Sampler):
    """Partially randmoized sampler

    1. Sort by lengths
    2. Pick a small patch and randomize it
    3. Permutate mini-batchs
    """

    def __init__(self, lengths, batch_size=16, batch_group_size=None,
                 permutate=True):
        self.lengths, self.sorted_indices = torch.sort(torch.LongTensor(lengths))
        self.batch_size = batch_size
        if batch_group_size is None:
            batch_group_size = min(batch_size * 32, len(self.lengths))
            if batch_group_size % batch_size != 0:
                batch_group_size -= batch_group_size % batch_size

        self.batch_group_size = batch_group_size
        assert batch_group_size % batch_size == 0
        self.permutate = permutate

    def __iter__(self):
        indices = self.sorted_indices.clone()
        batch_group_size = self.batch_group_size
        s, e = 0, 0
        for i in range(len(indices) // batch_group_size):
            s = i * batch_group_size
            e = s + batch_group_size
            random.shuffle(indices[s:e])

        # Permutate batches
        if self.permutate:
            perm = np.arange(len(indices[:e]) // self.batch_size)
            random.shuffle(perm)
            indices[:e] = indices[:e].view(-1, self.batch_size)[perm, :].view(-1)

        # Handle last elements
        s += batch_group_size
        if s < len(indices):
            random.shuffle(indices[s:])

        return iter(indices)

    def __len__(self):
        return len(self.sorted_indices)


class PyTorchDataset(object):
    def __init__(self, X, Mel):
        self.X = X
        self.Mel = Mel
        # alias
        self.multi_speaker = X.file_data_source.multi_speaker

    def __getitem__(self, idx):
        if self.Mel is None:
            mel = None
        else:
            mel = self.Mel[idx]

        raw_audio = self.X[idx]
        if self.multi_speaker:
            speaker_id = self.X.file_data_source.speaker_ids[idx]
        else:
            speaker_id = None

        # (x,c,g)
        return raw_audio, mel, speaker_id

    def __len__(self):
        return len(self.X)


def sequence_mask(sequence_length, max_len=None):
    if max_len is None:
        max_len = sequence_length.data.max()
    batch_size = sequence_length.size(0)
    seq_range = torch.arange(0, max_len).long()
    seq_range_expand = seq_range.unsqueeze(0).expand(batch_size, max_len)
    seq_range_expand = Variable(seq_range_expand, requires_grad=False)
    if sequence_length.is_cuda:
        seq_range_expand = seq_range_expand.cuda()
    seq_length_expand = sequence_length.unsqueeze(1) \
        .expand_as(seq_range_expand)
    return (seq_range_expand < seq_length_expand).float()


class MaskedCrossEntropyLoss(nn.Module):
    def __init__(self):
        super(MaskedCrossEntropyLoss, self).__init__()
        self.criterion = nn.CrossEntropyLoss(reduce=False)

    def forward(self, input, target, lengths=None, mask=None, max_len=None):
        if lengths is None and mask is None:
            raise RuntimeError("Should provide either lengths or mask")

        # (B, T, 1)
        if mask is None:
            mask = sequence_mask(lengths, max_len).unsqueeze(-1)

        # (B, T, D)
        mask_ = mask.expand_as(target)
        losses = self.criterion(input, target)
        return ((losses * mask_).sum()) / mask_.sum()


def ensure_divisible(length, divisible_by=256, lower=True):
    if length % divisible_by == 0:
        return length
    if lower:
        return length - length % divisible_by
    else:
        return length + (divisible_by - length % divisible_by)


def assert_ready_for_upsampling(x, c):
    assert len(x) % len(c) == 0 and len(x) // len(c) == audio.get_hop_size()


def collate_fn(batch):
    """Create batch

    Args:
        batch(tuple): List of tuples
            - x[0] (ndarray,int) : list of (T,)
            - x[1] (ndarray,int) : list of (T, D)
            - x[2] (ndarray,int) : list of (1,), speaker id
    Returns:
        tuple: Tuple of batch
            - x (FloatTensor) : Network inputs (B, C, T)
            - y (LongTensor)  : Network targets (B, T, 1)
    """

    local_conditioning = len(batch[0]) >= 2 and hparams.cin_channels > 0
    global_conditioning = len(batch[0]) >= 3 and hparams.gin_channels > 0

    # To save GPU memory... I don't want to do this though
    if hparams.max_time_sec is not None:
        max_time_steps = int(hparams.max_time_sec * hparams.sample_rate)
    elif hparams.max_time_steps is not None:
        max_time_steps = hparams.max_time_steps
    else:
        max_time_steps = None

    # Time resolution adjastment
    if local_conditioning:
        new_batch = []
        for idx in range(len(batch)):
            x, c, g = batch[idx]
            if hparams.upsample_conditional_features:
                assert_ready_for_upsampling(x, c)
                if max_time_steps is not None:
                    max_steps = ensure_divisible(max_time_steps, audio.get_hop_size(), True)
                    if len(x) > max_steps:
                        max_time_frames = max_steps // audio.get_hop_size()
                        s = np.random.randint(0, len(c) - max_time_frames)
                        ts = s * audio.get_hop_size()
                        x = x[ts:ts + audio.get_hop_size() * max_time_frames]
                        c = c[s:s + max_time_frames, :]
                        assert_ready_for_upsampling(x, c)
            else:
                x, c = audio.adjast_time_resolution(x, c)
                if max_time_steps is not None and len(x) > max_time_steps:
                    s = np.random.randint(0, len(x) - max_time_steps)
                    x, c = x[s:s + max_time_steps], c[s:s + max_time_steps, :]
                assert len(x) == len(c)
            new_batch.append((x, c, g))
        batch = new_batch
    else:
        new_batch = []
        for idx in range(len(batch)):
            x, c, g = batch[idx]
            x = audio.trim(x)
            if max_time_steps is not None and len(x) > max_time_steps:
                s = np.random.randint(0, len(x) - max_time_steps)
                x, c = x[s:s + max_time_steps], c[s:s + max_time_steps, :]
            new_batch.append((x, c, g))
        batch = new_batch

    # Lengths
    input_lengths = [len(x[0]) for x in batch]
    max_input_len = max(input_lengths)

    # (B, T, C)
    # pad for time-axis
    x_batch = np.array([_pad_2d(np_utils.to_categorical(x[0], num_classes=256),
                                max_input_len) for x in batch], dtype=np.float32)
    assert len(x_batch.shape) == 3

    # (B, T)
    y_batch = np.array([_pad(x[0], max_input_len) for x in batch], dtype=np.int)
    assert len(y_batch.shape) == 2

    # (B, T, D)
    if local_conditioning:
        max_len = max([len(x[1]) for x in batch])
        c_batch = np.array([_pad_2d(x[1], max_len) for x in batch], dtype=np.float32)
        assert len(c_batch.shape) == 3
        # (B x C x T)
        c_batch = torch.FloatTensor(c_batch).transpose(1, 2).contiguous()
    else:
        c_batch = None

    if global_conditioning:
        g_batch = torch.LongTensor([x[2] for x in batch])
    else:
        g_batch = None

    # Covnert to channel first i.e., (B, C, T)
    x_batch = torch.FloatTensor(x_batch).transpose(1, 2).contiguous()
    # Add extra axis
    y_batch = torch.LongTensor(y_batch).unsqueeze(-1).contiguous()

    input_lengths = torch.LongTensor(input_lengths)

    return x_batch, y_batch, c_batch, g_batch, input_lengths


def time_string():
    return datetime.now().strftime('%Y-%m-%d %H:%M')


def save_waveplot(path, y_hat, y_target):
    sr = hparams.sample_rate

    plt.figure(figsize=(16, 6))
    plt.subplot(2, 1, 1)
    librosa.display.waveplot(y_target, sr=sr)
    plt.subplot(2, 1, 2)
    librosa.display.waveplot(y_hat, sr=sr)
    plt.tight_layout()
    plt.savefig(path, format="png")
    plt.close()


def eval_model(global_step, writer, model, y, c, g, input_lengths, eval_dir):
    model.eval()
    idx = np.random.randint(0, len(y))
    length = input_lengths[idx].data.cpu().numpy()[0]

    # (T,)
    y_target = y[idx].view(-1).data.cpu().long().numpy()[:length]

    if c is not None:
        c = c[idx, :, :length].unsqueeze(0)
        assert c.dim() == 3
        print("Shape of local conditioning features: {}".format(c.size()))
    if g is not None:
        # TODO: test
        g = g[idx]
        print("Shape of global conditioning features: {}".format(g.size()))

    # Dummy silence
    initial_value = P.mulaw_quantize(0)
    print("Intial value:", initial_value)

    # (C,)
    initial_input = np_utils.to_categorical(initial_value, num_classes=256).astype(np.float32)
    initial_input = Variable(torch.from_numpy(initial_input), volatile=True).view(1, 1, 256)
    initial_input = initial_input.cuda() if use_cuda else initial_input
    y_hat = model.incremental_forward(
        initial_input, c=c, g=g, T=length, tqdm=tqdm, softmax=True, quantize=True)
    y_hat = y_hat.max(1)[1].view(-1).long().cpu().data.numpy()
    y_hat = P.inv_mulaw_quantize(y_hat)

    y_target = P.inv_mulaw_quantize(y_target)

    # Save audio
    os.makedirs(eval_dir, exist_ok=True)
    path = join(eval_dir, "step{:09d}_predicted.wav".format(global_step))
    librosa.output.write_wav(path, y_hat, sr=hparams.sample_rate)
    path = join(eval_dir, "step{:09d}_target.wav".format(global_step))
    librosa.output.write_wav(path, y_target, sr=hparams.sample_rate)

    # save figure
    path = join(eval_dir, "step{:09d}_waveplots.png".format(global_step))
    save_waveplot(path, y_hat, y_target)


def save_states(global_step, writer, y_hat, y, input_lengths, checkpoint_dir=None):
    print("Save intermediate states at step {}".format(global_step))
    idx = np.random.randint(0, len(y_hat))
    length = input_lengths[idx].data.cpu().numpy()[0]

    # (B, C, T)
    y_hat = y_hat.squeeze(-1)
    # (B, T)
    y_hat = F.softmax(y_hat, dim=1).max(1)[1]

    # (T,)
    y_hat = y_hat[idx].data.cpu().long().numpy()
    y = y[idx].view(-1).data.cpu().long().numpy()

    y_hat = P.inv_mulaw_quantize(y_hat)
    y = P.inv_mulaw_quantize(y)

    # Mask by length
    y_hat[length:] = 0
    y[length:] = 0

    # Save audio
    audio_dir = join(checkpoint_dir, "audio")
    os.makedirs(audio_dir, exist_ok=True)
    path = join(audio_dir, "step{:09d}_predicted.wav".format(global_step))
    librosa.output.write_wav(path, y_hat, sr=hparams.sample_rate)
    path = join(audio_dir, "step{:09d}_target.wav".format(global_step))
    librosa.output.write_wav(path, y, sr=hparams.sample_rate)


def __train_step(phase, epoch, global_step, global_test_step,
                 model, optimizer, writer, criterion,
                 x, y, c, g, input_lengths,
                 checkpoint_dir, eval_dir=None, do_eval=False):
    # x : (B, C, T)
    # y : (B, T, 1)
    # c : (B, C, T)
    # g : (B,)
    train = (phase == "train")
    clip_thresh = hparams.clip_thresh
    if train:
        model.train()
        step = global_step
    else:
        model.eval()
        step = global_test_step

    # Learning rate schedule
    current_lr = hparams.initial_learning_rate
    if train and hparams.lr_schedule is not None:
        lr_schedule_f = getattr(lrschedule, hparams.lr_schedule)
        current_lr = lr_schedule_f(
            hparams.initial_learning_rate, step, **hparams.lr_schedule_kwargs)
        for param_group in optimizer.param_groups:
            param_group['lr'] = current_lr
    optimizer.zero_grad()

    # Prepare data
    x, y = Variable(x), Variable(y, requires_grad=False)
    c = Variable(c) if c is not None else None
    g = Variable(g) if g is not None else None
    input_lengths = Variable(input_lengths)
    if use_cuda:
        x, y = x.cuda(), y.cuda()
        input_lengths = input_lengths.cuda()
        c = c.cuda() if c is not None else None
        g = g.cuda() if g is not None else None

    # (B, T, 1)
    mask = sequence_mask(input_lengths, max_len=x.size(-1)).unsqueeze(-1)
    mask = mask[:, 1:, :]

    # Apply model
    # NOTE: softmax is handled in F.cross_entrypy_loss
    y_hat = model(x, c=c, g=g, softmax=False)

    # wee need 4d inputs for spatial cross entropy loss
    # (B, C, T, 1)
    y_hat = y_hat.unsqueeze(-1)

    loss = criterion(y_hat[:, :, :-1, :], y[:, 1:, :], mask=mask)

    if train and step > 0 and step % hparams.checkpoint_interval == 0:
        save_states(step, writer, y_hat, y, input_lengths, checkpoint_dir)
        save_checkpoint(model, optimizer, step, checkpoint_dir, epoch)

    if do_eval:
        # NOTE: use train step (i.e., global_step) for filename
        eval_model(global_step, writer, model, y, c, g, input_lengths, eval_dir)

    # Update
    if train:
        loss.backward()
        if clip_thresh > 0:
            grad_norm = torch.nn.utils.clip_grad_norm(model.parameters(), clip_thresh)
        optimizer.step()

    # Logs
    writer.add_scalar("{} loss".format(phase), float(loss.data[0]), step)
    if train:
        if clip_thresh > 0:
            writer.add_scalar("gradient norm", grad_norm, step)
        writer.add_scalar("learning rate", current_lr, step)

    return loss.data[0]


def train_loop(model, data_loaders, optimizer, writer, checkpoint_dir=None):
    if use_cuda:
        model = model.cuda()

    criterion = MaskedCrossEntropyLoss()

    global global_step, global_epoch, global_test_step
    while global_epoch < hparams.nepochs:
        for phase, data_loader in data_loaders.items():
            train = (phase == "train")
            running_loss = 0.
            test_evaluated = False
            for step, (x, y, c, g, input_lengths) in tqdm(enumerate(data_loader)):
                # Whether to save eval (i.e., online decoding) result
                do_eval = False
                eval_dir = join(checkpoint_dir, "{}_eval".format(phase))
                # Do eval per eval_interval for train
                if train and global_step > 0 \
                        and global_step % hparams.train_eval_interval == 0:
                    do_eval = True
                # Do eval for test
                # NOTE: Decoding WaveNet is quite time consuming, so
                # do only once in a single epoch for testset
                if not train and not test_evaluated \
                        and global_epoch % hparams.test_eval_epoch_interval == 0:
                    do_eval = True
                    test_evaluated = True
                if do_eval:
                    print("[{}] Eval at train step {}".format(phase, global_step))

                # Do step
                running_loss += __train_step(
                    phase, global_epoch, global_step, global_test_step, model,
                    optimizer, writer, criterion, x, y, c, g, input_lengths,
                    checkpoint_dir, eval_dir, do_eval)

                # update global state
                if train:
                    global_step += 1
                else:
                    global_test_step += 1

            # log per epoch
            averaged_loss = running_loss / len(data_loader)
            writer.add_scalar("{} loss (per epoch)".format(phase),
                              averaged_loss, global_epoch)
            print("[{}] Loss: {}".format(phase, running_loss / len(data_loader)))

        global_epoch += 1


def save_checkpoint(model, optimizer, step, checkpoint_dir, epoch):
    checkpoint_path = join(
        checkpoint_dir, "checkpoint_step{:09d}.pth".format(global_step))
    optimizer_state = optimizer.state_dict() if hparams.save_optimizer_state else None
    global global_test_step
    torch.save({
        "state_dict": model.state_dict(),
        "optimizer": optimizer_state,
        "global_step": step,
        "global_epoch": epoch,
        "global_test_step": global_test_step,
    }, checkpoint_path)
    print("Saved checkpoint:", checkpoint_path)


def build_model():
    model = getattr(builder, hparams.builder)(
        layers=hparams.layers,
        stacks=hparams.stacks,
        residual_channels=hparams.residual_channels,
        gate_channels=hparams.gate_channels,
        skip_out_channels=hparams.skip_out_channels,
        cin_channels=hparams.cin_channels,
        gin_channels=hparams.gin_channels,
        weight_normalization=hparams.weight_normalization,
        n_speakers=hparams.n_speakers,
        dropout=hparams.dropout,
        kernel_size=hparams.kernel_size,
        upsample_conditional_features=hparams.upsample_conditional_features,
        upsample_scales=hparams.upsample_scales,
        freq_axis_kernel_size=hparams.freq_axis_kernel_size,
    )
    return model


def load_checkpoint(path, model, optimizer, reset_optimizer):
    global global_step
    global global_epoch
    global global_test_step

    print("Load checkpoint from: {}".format(path))
    checkpoint = torch.load(path)
    model.load_state_dict(checkpoint["state_dict"])
    if not reset_optimizer:
        optimizer_state = checkpoint["optimizer"]
        if optimizer_state is not None:
            print("Load optimizer state from {}".format(path))
            optimizer.load_state_dict(checkpoint["optimizer"])
    global_step = checkpoint["global_step"]
    global_epoch = checkpoint["global_epoch"]
    global_test_step = checkpoint.get("global_test_step", 0)

    return model


# https://discuss.pytorch.org/t/how-to-load-part-of-pre-trained-model/1113/3
def restore_parts(path, model):
    print("Restore part of the model from: {}".format(path))
    state = torch.load(path)["state_dict"]
    model_dict = model.state_dict()
    valid_state_dict = {k: v for k, v in state.items() if k in model_dict}
    model_dict.update(valid_state_dict)
    model.load_state_dict(model_dict)


def get_data_loaders(data_root, speaker_id, test_shuffle=True):
    data_loaders = {}
    local_conditioning = hparams.cin_channels > 0
    for phase in ["train", "test"]:
        train = phase == "train"
        X = FileSourceDataset(RawAudioDataSource(data_root, speaker_id=speaker_id,
                                                 train=train,
                                                 test_size=hparams.test_size,
                                                 test_num_samples=hparams.test_num_samples,
                                                 random_state=hparams.random_state))
        if local_conditioning:
            Mel = FileSourceDataset(MelSpecDataSource(data_root, speaker_id=speaker_id,
                                                      train=train,
                                                      test_size=hparams.test_size,
                                                      test_num_samples=hparams.test_num_samples,
                                                      random_state=hparams.random_state))
            assert len(X) == len(Mel)
            print("Local conditioning enabled. Shape of a sample: {}.".format(
                Mel[0].shape))
        else:
            Mel = None
        print("[{}]: length of the dataset is {}".format(phase, len(X)))

        if train:
            lengths = np.array(X.file_data_source.lengths)
            # Prepare sampler
            sampler = PartialyRandomizedSimilarTimeLengthSampler(
                lengths, batch_size=hparams.batch_size)
            shuffle = False
        else:
            sampler = None
            shuffle = test_shuffle

        dataset = PyTorchDataset(X, Mel)
        data_loader = data_utils.DataLoader(
            dataset, batch_size=hparams.batch_size,
            num_workers=hparams.num_workers, sampler=sampler, shuffle=shuffle,
            collate_fn=collate_fn, pin_memory=hparams.pin_memory)

        speaker_ids = {}
        for idx, (x, c, g) in enumerate(dataset):
            if g is not None:
                try:
                    speaker_ids[g] += 1
                except KeyError:
                    speaker_ids[g] = 1
        if len(speaker_ids) > 0:
            print("Speaker stats:", speaker_ids)

        data_loaders[phase] = data_loader

    return data_loaders


if __name__ == "__main__":
    args = docopt(__doc__)
    print("Command line args:\n", args)
    checkpoint_dir = args["--checkpoint-dir"]
    checkpoint_path = args["--checkpoint"]
    checkpoint_restore_parts = args["--restore-parts"]
    speaker_id = args["--speaker-id"]
    speaker_id = int(speaker_id) if speaker_id is not None else None

    data_root = args["--data-root"]
    if data_root is None:
        data_root = join(dirname(__file__), "data", "ljspeech")

    log_event_path = args["--log-event-path"]
    reset_optimizer = args["--reset-optimizer"]

    # Override hyper parameters
    hparams.parse(args["--hparams"])
    print(hparams_debug_string())
    assert hparams.name == "wavenet_vocoder"

    # Presets
    if hparams.preset is not None and hparams.preset != "":
        preset = hparams.presets[hparams.preset]
        import json
        hparams.parse_json(json.dumps(preset))
        print("Override hyper parameters with preset \"{}\": {}".format(
            hparams.preset, json.dumps(preset, indent=4)))

    os.makedirs(checkpoint_dir, exist_ok=True)

    # Dataloader setup
    data_loaders = get_data_loaders(data_root, speaker_id, test_shuffle=True)

    # Model
    model = build_model()
    print(model)
    if use_cuda:
        model = model.cuda()

    receptive_field = model.receptive_field
    print("Receptive field (samples / ms): {} / {}".format(
        receptive_field, receptive_field / fs * 1000))

    optimizer = optim.Adam(model.parameters(),
                           lr=hparams.initial_learning_rate, betas=(
        hparams.adam_beta1, hparams.adam_beta2),
        eps=hparams.adam_eps, weight_decay=hparams.weight_decay)

    if checkpoint_restore_parts is not None:
        restore_parts(checkpoint_restore_parts, model)

    # Load checkpoints
    if checkpoint_path is not None:
        load_checkpoint(checkpoint_path, model, optimizer, reset_optimizer)

    # Setup summary writer for tensorboard
    if log_event_path is None:
        log_event_path = "log/run-test" + str(datetime.now()).replace(" ", "_")
    print("Los event path: {}".format(log_event_path))
    writer = SummaryWriter(log_dir=log_event_path)

    # Train!
    try:
        train_loop(model, data_loaders, optimizer, writer, checkpoint_dir=checkpoint_dir)
    except KeyboardInterrupt:
        save_checkpoint(
            model, optimizer, global_step, checkpoint_dir, global_epoch)

    print("Finished")
    sys.exit(0)