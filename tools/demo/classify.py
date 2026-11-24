import json, sys
from dataclasses import replace
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # radice del progetto
from fotoscan.config import Config
from fotoscan.embedder import Embedder
raw = Path(sys.argv[1])
THEMES = {
 "mare": "una spiaggia con il mare e le onde", "montagna": "montagne rocciose con cime e valli",
 "neve": "un paesaggio innevato d'inverno", "lago": "un lago calmo circondato da natura",
 "bosco": "un bosco con alberi e sentieri", "citta": "una strada di una città con edifici",
 "architettura": "un edificio storico o un monumento", "notte": "una città di notte con luci",
 "cibo": "un piatto di cibo o una bevanda su un tavolo", "animale": "un animale, un cane o un gatto",
 "fiori": "fiori e piante in primo piano", "persona": "il ritratto di una persona",
 "oggetti": "un oggetto su una scrivania, un computer o un libro", "auto": "un'automobile o una moto",
 "tramonto": "un tramonto con il cielo arancione", "deserto": "un deserto o una strada lunga e vuota",
 "cielo": "il cielo con le nuvole o le stelle", "acqua": "una cascata o un fiume",
}
files = sorted(raw.glob("*.jpg"), key=lambda p: int(p.stem))
emb = Embedder(replace(Config(), model_name="google/siglip2-base-patch16-224", batch_size=64), device="cuda")
mat = emb.encode_arrays([emb.load_prep(str(p)) for p in files])
tv = emb.encode_texts(list(THEMES.values()))
s = mat @ tv.T
best = s.argmax(1)
names = list(THEMES)
out = {p.stem: names[b] for p, b in zip(files, best)}
json.dump(out, open(raw / "themes.json", "w"))
from collections import Counter
print(Counter(out.values()).most_common())
