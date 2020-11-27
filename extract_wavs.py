#!python
# coding: utf-8

import os
import argparse

import librosa
import soundfile


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("src_root", type=str, help="path to directory of containing source audio files, wav/mp3 supported."
    parser.add_argument("db_root", type=str, help="db root of wavenet_vocoder stage 0~2")
    parser.add_argument("sr", type=int, default=16000, help="sampling rate for output wavs")

    return parser.parse_args()


def extract_waves(src_root, db_root, sr):
    print("Start to extrac waves from {} to {} with sampling rate {}".format(src_root, db_root, sr))

    src_root = os.path.abspath(src_root)
    db_root = os.path.abspath(db_root)

    os.path.makedirs(db_root, exist_ok=True)

    if src_root.find(db_root) >= 0 or db_root.find(src_root) >= 0:
        print('Paths should not overlap.')
        exit(1)

    for root, dirs, files in os.walk(".", topdown=False):
        print('extracting in ' + root)
        for fn in files:
            extract_wav(os.path.join(root, fn), sr, db_root)

    print("Done.")


def extract_wav(audio_filepath, sr, db_root):
    y, y_sr = librosa.load(audio_filepath, sr=sr)

    fn_base = os.path.splitext(os.path.basename(audio_filepath))[0]
    soundfile.write(os.path.join(db_root, base, '.wav'), y, sr)

if '__name__' == __main__:
    extract_wavs(**vars(parse_args))

