hparams=./exp/levc_train_no_dev_levc_wavenet/hparams.json
checkpoint=./exp/levc_train_no_dev_levc_wavenet/checkpoint_step000300000_ema.pth
indir=~/data/LJSpeech-1.0/test_wavs

python ../../preprocess.py wavallin ${indir} ./data/ljspeech \
  --preset=${hparams}
