preset_json=./conf/levc_wavenet.json
checkpoint=./exp/levc_train_no_dev_levc_wavenet/checkpoint_step000300000_ema.pth
outdir=./exp/lj

#melsp=./data/ljspeech/LJ004-0218-feats.npy
melsp=./dump/levc/logmelspectrogram/norm/dev/D8_800-feats.npy

mkdir -p ${outdir}
python ../../synthesis.py --preset=${preset_json} \
  --conditional=$melsp \
  ${checkpoint} \
  ${outdir}
