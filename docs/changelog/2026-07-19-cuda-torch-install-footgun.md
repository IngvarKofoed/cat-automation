# Documented that ultralytics clobbers CUDA torch, Blackwell needs cu128

<!-- entry 20 -->

Documented the GPU-install footgun in `compute/requirements-analysis.txt`: `ultralytics`
pulls `torchvision`, which pins PyPI's CPU torch and silently clobbers a CUDA build, so
torch+torchvision must be installed together from the CUDA index. Blackwell GPUs (RTX
5060 Ti, sm_120) need cu128+ wheels — older CUDA wheels lack Blackwell kernels. `torchvision`
is now listed explicitly alongside torch.
