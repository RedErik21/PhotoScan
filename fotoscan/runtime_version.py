"""Versione del runtime (PyTorch, transformers e dipendenze pesanti) che questa app richiede.

Si alza SOLO quando cambia requirements-runtime.txt o la versione di Python della build: l'updater scarica il
runtime solo se la versione richiesta dall'app e' diversa da quella installata. Il test tests/test_updater.py
controlla che REQUIREMENTS_SHA256 corrisponda al file: se non coincide, hai modificato i requisiti senza alzare la versione.
"""
RUNTIME_VERSION = "1"
REQUIREMENTS_SHA256 = "2e6bfdda7527d56c625598279b6e7330d716da196905b424a4f58d98353b8bbf"
# Python con cui si costruiscono app e runtime (il runtime contiene estensioni compilate per questa versione).
PYTHON_TAG = "3.13"
# Moduli di primo livello che l'app leggera NON porta con se' (photoscan.spec li esclude): stanno nel runtime.
HEAVY_MODULES = ("torch", "torchvision", "torchgen", "functorch", "transformers", "accelerate", "tokenizers",
                 "safetensors", "huggingface_hub", "sentencepiece", "sympy", "mpmath", "networkx", "hf_xet")
# Pacchetti che l'app porta gia' per conto suo e che non si ripetono nel runtime. Vuoto di proposito: l'app ne ha solo
# i moduli che importa (es. PIL senza ImageEnhance, che serve a torchvision): il resto si completa dal runtime.
SHARED_WITH_APP = ()
