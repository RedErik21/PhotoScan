"""FotoScan: indicizzazione e organizzazione locale di archivi foto/video."""
from . import netguard as _netguard

_netguard.install()  # blocca la rete in uscita (solo 127.0.0.1) prima di ogni altro import
