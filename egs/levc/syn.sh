preset_json=./conf/levc_wavenet.json
checkpoint=./exp/levc_train_no_dev_levc_wavenet/checkpoint_step000300000_ema.pth

outdir=./exp/le_syn

melsp=./dump/levc/logmelspectrogram/norm/eval/pwangzhichao-pjulie-wave.npy
melsp=./dump/levc/logmelspectrogram/norm/dev/D8_864-feats.npy

mkdir -p ${outdir}
python ../../synthesis.py --preset=${preset_json} \
  --conditional=$melsp \
  ${checkpoint} \
  ${outdir}
