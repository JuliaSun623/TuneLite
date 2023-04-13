#export PATH="$PATH:/remote-home/ysun/cudas/bin"
#export CUDA_HOME="/remote-home/ysun/cudas/"
#export LD_LIBRARY_PATH="$LD_LIBRARY_PATH:/remote-home/ysun/cudas/lib64"

CUDA_VISIBLE_DEVICES=1,2 torchrun --nproc_per_node 2 --master_port=22233 train.py hf_args.yaml
