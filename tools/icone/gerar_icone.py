"""Gera o ícone e o logotipo do JFLP Studio (SVG + PNG + ICO).

Como rodar (o Pillow NÃO está no .venv do projeto; use um venv descartável
fora do repositório):

    python -m venv E:/JFLP/sdd/iconvenv
    E:/JFLP/sdd/iconvenv/Scripts/python -m pip install pillow
    E:/JFLP/sdd/iconvenv/Scripts/python tools/icone/gerar_icone.py

Rode a partir da raiz do repositório. Saídas:

    docs/img/logo.svg, docs/img/logo.png (512x512)
    sbfl/web/static/logo.svg, favicon.png (64x64), jflp.ico (16..256)

A geometria é definida uma única vez, numa grade de 512 unidades: um tile
azul-marinho arredondado, um "J" branco e, sob o gancho do J, três barras de
"suspeição" na escala verde -> âmbar -> vermelho. Dois conjuntos de medidas:
"cheio" (>= 64 px) e "compacto" (<= 48 px: J mais alto, barras mais grossas).
"""
import math
from pathlib import Path

from PIL import Image, ImageDraw

RAIZ = Path(__file__).resolve().parents[2]
G = 512                       # tamanho da grade
RAIO_TILE = 112
FUNDO_TOPO, FUNDO_BASE = "#1c2f57", "#0f1b33"
BRANCO = "#ffffff"
VERDE, AMBAR, VERMELHO = "#2fa862", "#f0a030", "#e5484d"


def arco_poligono(cx, cy, r_ext, r_int, passos=40):
    """Meia-coroa inferior (de 0 a 180 graus, y para baixo) como polígono."""
    ext = [(cx + r_ext * math.cos(math.pi * i / passos),
            cy + r_ext * math.sin(math.pi * i / passos)) for i in range(passos + 1)]
    int_ = [(cx + r_int * math.cos(math.pi * i / passos),
             cy + r_int * math.sin(math.pi * i / passos)) for i in range(passos, -1, -1)]
    return ext + int_


def geometria(compacto):
    """Lista de formas ('rrect', x0, y0, x1, y1, raio, cor) ou ('poly', pts, cor)."""
    cx, R, r = 256, 100, 32
    if compacto:
        topo, cy, h_barra, passo, y_barras, raio_b = 56, 210, 36, 50, 340, 6
    else:
        topo, cy, h_barra, passo, y_barras, raio_b = 64, 238, 24, 38, 372, 8
    esq, dir_ = cx - R, cx + R            # extremos do J (156 e 356)
    formas = [
        ("rrect", cx - 56, topo, dir_, topo + 36, 6, BRANCO),            # barra superior
        ("rrect", cx + r, topo, dir_, cy, 0, BRANCO),                    # haste
        ("poly", arco_poligono(cx, cy, R, r), BRANCO),                   # gancho
        ("rrect", esq, cy - 42, cx - r, cy, 0, BRANCO),                  # ponta do gancho
    ]
    for i, cor in enumerate((VERDE, AMBAR, VERMELHO)):
        y = y_barras + i * passo
        formas.append(("rrect", esq, y, dir_, y + h_barra, raio_b, cor))
    return formas


def _n(v):
    return f"{v:.1f}".rstrip("0").rstrip(".")


def svg(compacto=False):
    p = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {G} {G}" width="{G}" height="{G}">',
         '<defs><linearGradient id="f" x1="0" y1="0" x2="0" y2="1">'
         f'<stop offset="0" stop-color="{FUNDO_TOPO}"/><stop offset="1" stop-color="{FUNDO_BASE}"/>'
         '</linearGradient></defs>',
         f'<rect width="{G}" height="{G}" rx="{RAIO_TILE}" fill="url(#f)"/>']
    for f in geometria(compacto):
        if f[0] == "rrect":
            _, x0, y0, x1, y1, rr, cor = f
            rx = f' rx="{rr}"' if rr else ""
            p.append(f'<rect x="{_n(x0)}" y="{_n(y0)}" width="{_n(x1 - x0)}" '
                     f'height="{_n(y1 - y0)}"{rx} fill="{cor}"/>')
        else:
            pts = " ".join(f"{_n(x)},{_n(y)}" for x, y in f[1])
            p.append(f'<polygon points="{pts}" fill="{f[2]}"/>')
    p.append("</svg>")
    return "\n".join(p) + "\n"


def png(tam):
    """Rasteriza com supersample 4x e reduz com LANCZOS."""
    s = 4
    big = tam * s
    k = big / G
    fundo = Image.new("RGBA", (big, big))
    d = ImageDraw.Draw(fundo)
    ca, cb = (tuple(int(h[i:i + 2], 16) for i in (1, 3, 5)) for h in (FUNDO_TOPO, FUNDO_BASE))
    for y in range(big):
        t = y / (big - 1)
        d.line([(0, y), (big, y)], fill=tuple(round(a + (b - a) * t) for a, b in zip(ca, cb)) + (255,))
    mascara = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mascara).rounded_rectangle([0, 0, big - 1, big - 1], RAIO_TILE * k, fill=255)
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    img.paste(fundo, (0, 0), mascara)
    d = ImageDraw.Draw(img)
    for f in geometria(tam <= 48):
        if f[0] == "rrect":
            _, x0, y0, x1, y1, rr, cor = f
            box = [x0 * k, y0 * k, x1 * k - 1, y1 * k - 1]
            if rr:
                d.rounded_rectangle(box, rr * k, fill=cor)
            else:
                d.rectangle(box, fill=cor)
        else:
            d.polygon([(x * k, y * k) for x, y in f[1]], fill=f[2])
    return img.resize((tam, tam), Image.LANCZOS)


def main():
    logo = svg(False)
    for destino in ("docs/img/logo.svg", "sbfl/web/static/logo.svg"):
        (RAIZ / destino).write_text(logo, encoding="utf-8")
    png(512).save(RAIZ / "docs/img/logo.png")
    png(64).save(RAIZ / "sbfl/web/static/favicon.png")
    tamanhos = [16, 24, 32, 48, 64, 128, 256]
    # o Pillow reamostra a imagem-fonte; para manter cada tamanho nítido,
    # anexa os quadros já renderizados individualmente.
    quadros = [png(t) for t in tamanhos]
    quadros[-1].save(RAIZ / "sbfl/web/static/jflp.ico", format="ICO", sizes=[(t, t) for t in tamanhos],
                     append_images=quadros[:-1])
    # prévias para inspeção (não versionadas): passe --previas DIR
    import sys
    if "--previas" in sys.argv:
        pasta = Path(sys.argv[sys.argv.index("--previas") + 1])
        pasta.mkdir(parents=True, exist_ok=True)
        for t in (16, 24, 32, 48, 64, 256):
            png(t).save(pasta / f"p{t}.png")


if __name__ == "__main__":
    main()
