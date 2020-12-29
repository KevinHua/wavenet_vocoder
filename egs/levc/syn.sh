preset_json=./conf/levc_wavenet.json
checkpoint=./exp/levc_train_no_dev_levc_wavenet/checkpoint_latest_ema.pth

outdir=./exp/autovc_syn

melsp=./dump/levc/logmelspectrogram/norm/autovc/pjulie-pjulie-wave.npy

mkdir -p ${outdir}
python ../../synthesis.py --preset=${preset_json} \
  --conditional=$melsp \
  ${checkpoint} \
  ${outdir}
