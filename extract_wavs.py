#!python
# coding: utf-8

import os
import argparse

import librosa
import soundfile


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("src_root", type=str, help="path to directory of containing source audio files, wav/mp3 supported.")
    parser.add_argument("db_root", type=str, help="db root of wavenet_vocoder stage 0~2")
    parser.add_argument("sr", type=int, default=16000, help="sampling rate for output wavs")

    return parser.parse_args()


def extract_wavs(src_root, db_root, sr):
    print("Start to extrac waves from {} to {} with sampling rate {}".format(src_root, db_root, sr))

    src_root = os.path.abspath(src_root)
    db_root = os.path.abspath(db_root)

    os.makedirs(db_root, exist_ok=True)

    if src_root.find(db_root) >= 0 or db_root.find(src_root) >= 0:
        print('Paths should not overlap.')
        exit(1)

    for root, dirs, files in os.walk(src_root, topdown=False, followlinks=True):
        print('extracting in ' + root)
        for fn in files:
            try:
                audio_filepath = os.path.join(root, fn)
                fn_base = os.path.splitext(os.path.basename(audio_filepath))[0]
                wav_filepath = os.path.join(db_root, fn_base + '.wav')
                if not os.path.exists(wav_filepath):
                    extract_wav(audio_filepath, sr, wav_filepath)
            except SystemExit:
                break
            except Exception as e:
                print(e)

    print("Done.")


def extract_wav(audio_filepath, sr, wav_filepath):
    print('extract {}'.format(audio_filepath))

    y, y_sr = librosa.load(audio_filepath, sr=sr)
    soundfile.write(wav_filepath, y, sr)

if __name__ == '__main__':
    extract_wavs(**vars(parse_args()))

