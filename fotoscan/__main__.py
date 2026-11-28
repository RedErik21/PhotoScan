from . import runtime

runtime.activate()  # PyTorch dal runtime a parte (nell'exe lo fa gia' photoscan.py; idempotente)

from .cli import main

main()
