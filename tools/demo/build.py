"""Libreria demo per screenshot/video promozionali: nessun dato personale.
Foto: Picsum/Unsplash (licenza Unsplash). Date, GPS, screenshot, scontrini e video sono generati."""
import json, random, shutil, subprocess, sys
from datetime import datetime, timedelta
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageEnhance, ImageFilter

RAW = Path(sys.argv[1]); LIB = Path(sys.argv[2]); FF = sys.argv[3]
rng = random.Random(42)
themes = json.load(open(RAW / "themes.json"))
by_theme = {}
for k, t in sorted(themes.items(), key=lambda x: int(x[0])):
    by_theme.setdefault(t, []).append(k)
for v in by_theme.values():
    rng.shuffle(v)

HOME = (45.4642, 9.1900)  # Milano
TRIPS = [  # nome, inizio, giorni, gps, jitter, {tema: quante}, cartella, camera
    ("sardegna", datetime(2024, 8, 5), 9, (39.14, 9.52), 0.05, {"mare": 70, "tramonto": 25, "cielo": 6}, "Telefono", ("Google", "Pixel 8")),
    ("cortina", datetime(2023, 2, 10), 5, (46.54, 12.14), 0.04, {"neve": 26, "montagna": 15}, "Fotocamera", ("Sony", "ILCE-7M3")),
    ("dolomiti", datetime(2022, 7, 20), 7, (46.43, 11.80), 0.06, {"montagna": 35, "lago": 25, "bosco": 25}, "Fotocamera", ("Sony", "ILCE-7M3")),
    ("roma", datetime(2023, 4, 22), 4, (41.895, 12.48), 0.02, {"architettura": 40, "citta": 12}, "Telefono", ("Google", "Pixel 7")),
    ("parigi", datetime(2022, 10, 7), 4, (48.857, 2.345), 0.02, {"architettura": 35, "citta": 15, "notte": 12}, "Telefono", ("Google", "Pixel 7")),
    ("newyork", datetime(2025, 5, 1), 7, (40.75, -73.98), 0.03, {"citta": 22, "notte": 20, "architettura": 30}, "Telefono", ("Google", "Pixel 8")),
    ("islanda", datetime(2021, 6, 10), 10, (64.25, -20.5), 0.9, {"deserto": 30, "acqua": 21, "cielo": 25, "lago": 30}, "Fotocamera", ("Sony", "ILCE-7M3")),
    ("como", datetime(2024, 5, 18), 2, (45.99, 9.26), 0.03, {"lago": 30, "fiori": 10}, "Telefono", ("Google", "Pixel 8")),
    ("toscana", datetime(2023, 9, 15), 4, (43.35, 11.33), 0.08, {"bosco": 40, "fiori": 12, "tramonto": 20}, "Telefono", ("Google", "Pixel 7")),
]


def exif_bytes(dt, gps, cam):
    ex = Image.Exif()
    ex[271], ex[272] = cam
    ex[306] = dt.strftime("%Y:%m:%d %H:%M:%S")
    ex.get_ifd(0x8769)[36867] = dt.strftime("%Y:%m:%d %H:%M:%S")
    if gps:
        def dms(x):
            x = abs(x); d = int(x); m = int((x - d) * 60); s = round(((x - d) * 60 - m) * 60, 2)
            return (float(d), float(m), float(s))
        g = ex.get_ifd(0x8825)
        g[1] = "N" if gps[0] >= 0 else "S"; g[2] = dms(gps[0])
        g[3] = "E" if gps[1] >= 0 else "W"; g[4] = dms(gps[1])
    return ex.tobytes()


def fname(dt, cam):
    return f"IMG_{dt:%Y%m%d_%H%M%S}.jpg" if cam[0] != "Sony" else f"DSC{rng.randint(1000, 9999):04d}.JPG"


def save(src_img, folder, dt, gps, cam, name=None, quality=90):
    d = LIB / folder; d.mkdir(parents=True, exist_ok=True)
    p = d / (name or fname(dt, cam))
    while p.exists():
        dt += timedelta(seconds=1); p = d / fname(dt, cam)
    src_img.convert("RGB").save(p, quality=quality, exif=exif_bytes(dt, gps, cam))
    return p


def times(start, days, n):
    return sorted(start + timedelta(days=rng.randrange(days), hours=rng.randint(8, 20), minutes=rng.randint(0, 59),
                                    seconds=rng.randint(0, 59)) for _ in range(n))


if LIB.exists():
    shutil.rmtree(LIB)
LIB.mkdir(parents=True)
made = []   # (path, dt, gps, cam, raw_id)
for name, start, days, gps0, jit, want, folder, cam in TRIPS:
    ids = []
    for t, n in want.items():
        ids += [by_theme[t].pop() for _ in range(min(n, len(by_theme[t])))]
    rng.shuffle(ids)
    for i, dt in zip(ids, times(start, days, len(ids))):
        gps = (gps0[0] + rng.uniform(-jit, jit), gps0[1] + rng.uniform(-jit, jit) * (2 if jit > 0.5 else 1))
        p = save(Image.open(RAW / f"{i}.jpg"), f"{folder}/{start.year}", dt, gps, cam)
        made.append((p, dt, gps, cam, i))

# casa: il resto (cibo, animali, oggetti, fiori, auto, persone...) sparso su 2019-2025
rest = [i for v in by_theme.values() for i in v]
rng.shuffle(rest)
undated, home = rest[:40], rest[40:]
for i in home:
    dt = datetime(2019, 1, 1) + timedelta(days=rng.randrange(0, 2500), hours=rng.randint(8, 21), minutes=rng.randint(0, 59))
    gps = (HOME[0] + rng.uniform(-0.04, 0.04), HOME[1] + rng.uniform(-0.05, 0.05)) if rng.random() < 0.8 else None
    p = save(Image.open(RAW / f"{i}.jpg"), f"Telefono/{dt.year}", dt, gps, ("Google", "Pixel 7"))
    made.append((p, dt, gps, ("Google", "Pixel 7"), i))
# senza data (vecchie scansioni): niente EXIF, nome senza data
d = LIB / "Vecchie scansioni"; d.mkdir()
for n, i in enumerate(undated):
    Image.open(RAW / f"{i}.jpg").convert("RGB").save(d / f"scansione_{n + 1:03d}.jpg", quality=88)

# duplicati esatti (backup del telefono) e versioni modificate
bk = LIB / "Backup vecchio telefono"; bk.mkdir()
for p, *_ in rng.sample(made, 30):
    shutil.copy2(p, bk / p.name)
for p, dt, gps, cam, i in rng.sample(made, 12):
    im = ImageEnhance.Color(ImageEnhance.Contrast(Image.open(p)).enhance(1.15)).enhance(1.2)
    im.save(p.with_name(p.stem + "-modificato" + p.suffix), quality=92, exif=exif_bytes(dt, gps, cam))
# raffiche: secondo scatto quasi identico pochi secondi dopo
for p, dt, gps, cam, i in rng.sample(made, 15):
    im = Image.open(p); w, h = im.size
    im = im.crop((int(w * 0.01), int(h * 0.01), int(w * 0.995), int(h * 0.995))).resize((w, h))
    save(im, p.parent.relative_to(LIB).as_posix(), dt + timedelta(seconds=rng.randint(1, 6)), gps, cam)
# foto molto pesanti (>= 8 MB)
for p, dt, gps, cam, i in rng.sample([m for m in made if m[3][0] == "Sony"], 4):
    src = Image.open(p)
    im = src.resize((7200, round(7200 * src.height / src.width)), Image.LANCZOS).filter(ImageFilter.UnsharpMask(2, 150, 0))
    save(im, p.parent.relative_to(LIB).as_posix(), dt + timedelta(seconds=30), gps, cam, name=p.stem + "_RAW.JPG", quality=100)

# ---------------------------------------------------------------- screenshot di telefono (inventati)
FONTS = "C:/Windows/Fonts/"
def font(n, s):
    return ImageFont.truetype(FONTS + n, s)
SEG, SEGB, MONO = "segoeui.ttf", "segoeuib.ttf", "consola.ttf"
PAL = [(26, 115, 232), (52, 168, 83), (234, 67, 53), (251, 188, 5), (103, 58, 183), (0, 150, 136)]
CHAT = ["Ci vediamo alle 8?", "Perfetto, prenoto io", "Hai visto le foto del weekend?", "Bellissime!!", "Domani piove, rimandiamo?",
        "Arrivo tra 10 minuti", "Va bene", "Mandami la posizione", "Che ristorante era?", "Buon compleanno!", "Grazie mille", "A che ora parte il treno?"]
SETTINGS = ["Wi-Fi", "Bluetooth", "Rete e Internet", "Dispositivi connessi", "App", "Notifiche", "Batteria", "Spazio di archiviazione",
            "Audio e vibrazione", "Display", "Sfondo e stile", "Accessibilità", "Sicurezza", "Privacy", "Posizione", "Sistema"]


def status_bar(d, W, dark):
    c = (240, 240, 240) if dark else (30, 30, 30)
    d.text((48, 28), f"{rng.randint(7, 23)}:{rng.randint(0, 59):02d}", font=font(SEGB, 38), fill=c)
    d.text((W - 200, 28), "5G  86%", font=font(SEG, 34), fill=c)


def shot_chat(W, H):
    dark = rng.random() < 0.4
    im = Image.new("RGB", (W, H), (18, 18, 18) if dark else (236, 229, 221)); d = ImageDraw.Draw(im); status_bar(d, W, dark)
    d.rectangle((0, 100, W, 260), fill=(32, 44, 51) if dark else (0, 128, 105))
    d.ellipse((40, 125, 150, 235), fill=rng.choice(PAL))
    d.text((180, 140), rng.choice(["Gruppo Vacanze", "Calcetto giovedì", "Famiglia", "Viaggio 2024"]), font=font(SEGB, 46), fill="white")
    d.text((180, 198), "online", font=font(SEG, 32), fill=(220, 220, 220))
    y = 320
    while y < H - 300:
        me = rng.random() < 0.5; t = rng.choice(CHAT)
        f = font(SEG, 44); w = d.textlength(t, font=f) + 60
        x0 = W - w - 40 if me else 40
        fill = ((0, 92, 75) if dark else (217, 253, 211)) if me else ((32, 44, 51) if dark else (255, 255, 255))
        d.rounded_rectangle((x0, y, x0 + w, y + 100), 28, fill=fill)
        d.text((x0 + 30, y + 22), t, font=f, fill=(235, 235, 235) if dark else (20, 20, 20))
        y += 140 + rng.randint(0, 40)
    d.rounded_rectangle((30, H - 190, W - 160, H - 80), 55, fill=(32, 44, 51) if dark else (255, 255, 255))
    d.text((80, H - 160), "Messaggio", font=font(SEG, 42), fill=(140, 140, 140))
    d.ellipse((W - 140, H - 190, W - 30, H - 80), fill=(0, 168, 132))
    return im


def shot_settings(W, H):
    im = Image.new("RGB", (W, H), (248, 249, 250)); d = ImageDraw.Draw(im); status_bar(d, W, False)
    d.text((60, 160), "Impostazioni", font=font(SEGB, 84), fill=(32, 33, 36))
    d.rounded_rectangle((50, 320, W - 50, 440), 60, fill=(232, 234, 237)); d.text((120, 350), "Cerca nelle impostazioni", font=font(SEG, 42), fill=(95, 99, 104))
    y = 520
    for s in rng.sample(SETTINGS, 11):
        d.ellipse((70, y, 160, y + 90), fill=rng.choice(PAL))
        d.text((200, y + 2), s, font=font(SEG, 48), fill=(32, 33, 36)); d.text((200, y + 60), "Tocca per gestire", font=font(SEG, 32), fill=(95, 99, 104))
        y += 160
    return im


def shot_map(W, H):
    im = Image.new("RGB", (W, H), (233, 229, 220)); d = ImageDraw.Draw(im)
    for _ in range(14):
        x, y = rng.randint(0, W), rng.randint(0, H)
        d.rectangle((x, y, x + rng.randint(100, 400), y + rng.randint(80, 300)), fill=rng.choice([(200, 230, 201), (170, 218, 255), (245, 241, 232)]))
    for _ in range(12):
        d.line((rng.randint(0, W), rng.randint(0, H), rng.randint(0, W), rng.randint(0, H)), fill="white", width=rng.choice([18, 26, 34]))
    d.line((200, H - 600, 500, 1400, 800, 900), fill=(26, 115, 232), width=22, joint="curve")
    status_bar(d, W, False)
    d.rounded_rectangle((40, 110, W - 40, 230), 60, fill="white"); d.text((110, 140), "Cerca qui", font=font(SEG, 44), fill=(95, 99, 104))
    d.rounded_rectangle((0, H - 520, W, H), 50, fill="white")
    d.text((60, H - 470), f"{rng.randint(12, 48)} min ({rng.randint(4, 30)} km)", font=font(SEGB, 58), fill=(24, 128, 56))
    d.text((60, H - 380), "Percorso più veloce, traffico regolare", font=font(SEG, 40), fill=(95, 99, 104))
    d.rounded_rectangle((60, H - 260, 460, H - 140), 60, fill=(26, 115, 232)); d.text((150, H - 235), "Avvia", font=font(SEGB, 50), fill="white")
    return im


def shot_shop(W, H):
    im = Image.new("RGB", (W, H), "white"); d = ImageDraw.Draw(im); status_bar(d, W, False)
    d.text((60, 140), rng.choice(["Il tuo carrello", "Offerte del giorno", "Ordini recenti"]), font=font(SEGB, 70), fill=(20, 20, 20))
    y = 300
    for _ in range(5):
        d.rounded_rectangle((60, y, 360, y + 300), 30, fill=rng.choice([(255, 224, 178), (200, 230, 201), (187, 222, 251), (248, 187, 208)]))
        d.text((400, y + 20), rng.choice(["Scarpe da corsa", "Cuffie wireless", "Zaino da viaggio", "Borraccia termica", "Lampada da tavolo", "Libro di cucina"]), font=font(SEGB, 46), fill=(20, 20, 20))
        d.text((400, y + 90), f"{rng.randint(9, 129)},{rng.choice(['90', '99', '00'])} €", font=font(SEG, 52), fill=(177, 39, 4))
        d.text((400, y + 170), "Consegna gratuita domani", font=font(SEG, 34), fill=(0, 118, 0))
        y += 360
    return im


ss = LIB / "Telefono" / "Screenshots"; ss.mkdir(parents=True)
for n in range(34):
    dt = datetime(2020, 1, 1) + timedelta(days=rng.randrange(0, 2150), hours=rng.randint(8, 23), minutes=rng.randint(0, 59), seconds=rng.randint(0, 59))
    rng.choice([shot_chat, shot_chat, shot_settings, shot_map, shot_shop])(1080, 2400).save(ss / f"Screenshot_{dt:%Y%m%d-%H%M%S}.png")

# ---------------------------------------------------------------- scontrini fotografati (inventati)
SHOPS = ["SUPERMERCATO DEMO", "BAR CENTRALE", "FARMACIA DEL CORSO", "PIZZERIA DA NINO", "FERRAMENTA ROSSI", "LIBRERIA PAGINA 12"]
ITEMS = ["PANE", "LATTE PS", "CAFFE'", "MOZZARELLA", "POMODORI", "ACQUA NAT 6X1,5", "PASTA 500G", "BISCOTTI", "MELE KG", "OLIO EVO",
         "YOGURT", "CROISSANT", "CAPPUCCINO", "SPREMUTA"]


def receipt():
    w, h = 620, rng.randint(1100, 1500)
    paper = Image.new("RGB", (w, h), (250, 249, 244)); d = ImageDraw.Draw(paper); f = font(MONO, 30); fb = font(MONO, 36)
    shop = rng.choice(SHOPS); d.text((w / 2 - d.textlength(shop, font=fb) / 2, 50), shop, font=fb, fill=(40, 40, 40))
    d.text((150, 100), "Via Esempio 1 - Milano", font=f, fill=(60, 60, 60)); d.text((150, 140), "P.IVA 00000000000", font=f, fill=(60, 60, 60))
    y, tot = 230, 0
    for _ in range(rng.randint(5, 12)):
        it = rng.choice(ITEMS); pr = rng.randint(60, 990) / 100; tot += pr
        d.text((40, y), it, font=f, fill=(40, 40, 40)); d.text((w - 170, y), f"{pr:6.2f}", font=f, fill=(40, 40, 40)); y += 48
    d.line((40, y + 10, w - 40, y + 10), fill=(90, 90, 90), width=2)
    d.text((40, y + 30), "TOTALE EURO", font=fb, fill=(20, 20, 20)); d.text((w - 190, y + 30), f"{tot:7.2f}", font=fb, fill=(20, 20, 20))
    d.text((40, y + 110), "PAGAMENTO CARTA", font=f, fill=(60, 60, 60)); d.text((40, y + 160), "GRAZIE E ARRIVEDERCI", font=f, fill=(60, 60, 60))
    bg = Image.new("RGB", (1600, 2000), rng.choice([(120, 90, 60), (90, 90, 95), (160, 130, 100), (60, 70, 80)]))
    ang = rng.uniform(-7, 7)
    rot = paper.rotate(ang, expand=True, resample=Image.BICUBIC)
    m = Image.new("L", paper.size, 255).rotate(ang, expand=True, resample=Image.BICUBIC)
    bg.paste(rot, ((1600 - rot.width) // 2 + rng.randint(-80, 80), (2000 - rot.height) // 2 + rng.randint(-60, 60)), m)
    return ImageEnhance.Brightness(bg.filter(ImageFilter.GaussianBlur(0.8))).enhance(rng.uniform(0.9, 1.1))


for n in range(16):
    dt = datetime(2020, 6, 1) + timedelta(days=rng.randrange(0, 1900), hours=rng.randint(9, 20), minutes=rng.randint(0, 59))
    save(receipt(), f"Telefono/{dt.year}", dt, (HOME[0] + rng.uniform(-0.02, 0.02), HOME[1] + rng.uniform(-0.02, 0.02)), ("Google", "Pixel 7"))


# ---------------------------------------------------------------- video (zoom lento su foto dei viaggi)
def video(imgs, out, dt, secs, size, bitrate):
    parts = []
    for p in imgs:
        parts += ["-i", str(p)]   # una sola immagine: zoompan genera d fotogrammi per ogni fotogramma in ingresso
    n = len(imgs); W, H = size
    fl = "".join(f"[{k}:v]scale={W * 2}:{H * 2}:force_original_aspect_ratio=increase,crop={W * 2}:{H * 2},"
                 f"zoompan=z='min(zoom+0.0008,1.15)':d={secs * 30}:s={W}x{H}:fps=30,setsar=1[v{k}];" for k in range(n))
    fl += "".join(f"[v{k}]" for k in range(n)) + f"concat=n={n}:v=1:a=0,format=yuv420p[o]"
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([FF, "-y", "-loglevel", "error", *parts, "-filter_complex", fl, "-map", "[o]", "-c:v", "libx264", "-b:v", bitrate,
                    "-metadata", f"creation_time={dt:%Y-%m-%dT%H:%M:%S}Z", str(out)], check=True)


vids = 0
for name, start, days, gps0, jit, want, folder, cam in TRIPS:
    pool = [m for m in made if start <= m[1] <= start + timedelta(days=days)]
    if len(pool) < 3:
        continue
    sel = rng.sample(pool, 3); dt = sel[0][1] + timedelta(minutes=5)
    big = name in ("islanda", "sardegna")
    video([s[0] for s in sel], LIB / folder / str(start.year) / f"VID_{dt:%Y%m%d_%H%M%S}.mp4", dt, 7 if big else 3,
          (3840, 2160) if big else (1920, 1080), "24M" if big else "6M")
    vids += 1
print("foto", len(list(LIB.rglob("*.jpg"))) + len(list(LIB.rglob("*.JPG"))), "png", len(list(LIB.rglob("*.png"))), "video", vids)
