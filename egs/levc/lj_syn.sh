preset_json=./conf/levc_wavenet.json
checkpoint=./exp/levc_train_no_dev_levc_wavenet/checkpoint_step001000000_ema.pth
outdir=./exp/lj

#melsp=./data/ljspeech/LJ004-0218-feats.npy
melsp=./data/ljspeech/LJ004-0218-feats.npy

mkdir -p ${outdir}
python ../../synthesis.py --preset=${preset_json} \
  --conditional=$melsp \
  ${checkpoint} \
  ${outdir}
