import io
import textwrap

import streamlit as st
from PIL import Image, ImageDraw, ImageFont, ImageFilter

st.set_page_config(page_title="KywaTrace Case Example Generator", layout="wide")

BRAND_YELLOW = "#F2D400"
BRAND_DARK = "#111111"
BRAND_LIGHT = "#F6F6F2"
BRAND_MID = "#D8D8D3"
BRAND_WHITE = "#FFFFFF"

def hex_to_rgb(value: str):
    value = value.lstrip("#")
    return tuple(int(value[i:i+2], 16) for i in (0, 2, 4))

def get_font(size: int, bold: bool = False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()

def wrap_text(draw, text, font, max_width):
    words = text.split()
    if not words:
        return [""]
    lines, line = [], words[0]
    for word in words[1:]:
        trial = f"{line} {word}"
        if draw.textbbox((0, 0), trial, font=font)[2] <= max_width:
            line = trial
        else:
            lines.append(line)
            line = word
    lines.append(line)
    return lines

def draw_wrapped(draw, text, xy, font, fill, max_width, line_gap=8):
    x, y = xy
    bbox = draw.textbbox((0, 0), "Ag", font=font)
    line_h = bbox[3] - bbox[1] + line_gap
    for line in wrap_text(draw, text, font, max_width):
        draw.text((x, y), line, font=font, fill=fill)
        y += line_h
    return y

def pct_box(w, h, left, top, right, bottom):
    x1 = int(w * left / 100)
    y1 = int(h * top / 100)
    x2 = int(w * right / 100)
    y2 = int(h * bottom / 100)
    x1, x2 = sorted((max(0, x1), min(w, x2)))
    y1, y2 = sorted((max(0, y1), min(h, y2)))
    return (x1, y1, x2, y2)

def redact(img, box, mode):
    out = img.copy()
    if box[2] <= box[0] or box[3] <= box[1]:
        return out
    if mode == "Blur":
        region = out.crop(box).filter(ImageFilter.GaussianBlur(radius=18))
        out.paste(region, box)
    else:
        draw = ImageDraw.Draw(out)
        draw.rectangle(box, fill=hex_to_rgb(BRAND_DARK))
    return out

def crop_by_pct(img, l, t, r, b):
    w, h = img.size
    box = pct_box(w, h, l, t, r, b)
    if box[2] - box[0] < 10 or box[3] - box[1] < 10:
        return img
    return img.crop(box)

def add_highlight(img, box, thickness=8):
    out = img.copy()
    draw = ImageDraw.Draw(out)
    if box[2] > box[0] and box[3] > box[1]:
        draw.rectangle(box, outline=hex_to_rgb(BRAND_YELLOW), width=thickness)
    return out

def fit_image(img, target_w, target_h):
    img = img.copy()
    ratio = min(target_w / img.width, target_h / img.height)
    new_size = (max(1, int(img.width * ratio)), max(1, int(img.height * ratio)))
    img = img.resize(new_size, Image.LANCZOS)
    canvas = Image.new("RGB", (target_w, target_h), hex_to_rgb(BRAND_WHITE))
    x = (target_w - img.width) // 2
    y = (target_h - img.height) // 2
    canvas.paste(img, (x, y))
    return canvas

def build_card(fragment, audience, issue, requirement, project, impact, footer, logo=None):
    W, H = 1080, 1350
    img = Image.new("RGB", (W, H), hex_to_rgb(BRAND_LIGHT))
    draw = ImageDraw.Draw(img)

    draw.rectangle((0, 0, W, 130), fill=hex_to_rgb(BRAND_DARK))
    title_font = get_font(42, True)
    small_font = get_font(22, False)
    label_font = get_font(24, True)
    body_font = get_font(28, False)
    body_bold = get_font(28, True)
    footer_font = get_font(19, False)

    draw.text((50, 38), "KywaTrace", font=title_font, fill=hex_to_rgb(BRAND_WHITE))
    draw.rectangle((50, 98, 245, 108), fill=hex_to_rgb(BRAND_YELLOW))
    draw.text((720, 48), audience.upper(), font=label_font, fill=hex_to_rgb(BRAND_YELLOW))

    panel_x1, panel_y1, panel_x2, panel_y2 = 50, 175, 1030, 770
    draw.rounded_rectangle((panel_x1, panel_y1, panel_x2, panel_y2), radius=20, fill=hex_to_rgb(BRAND_WHITE))
    fitted = fit_image(fragment, panel_x2-panel_x1-30, panel_y2-panel_y1-30)
    img.paste(fitted, (panel_x1+15, panel_y1+15))

    y = 810
    draw.text((50, y), "KONSTATĒTS", font=label_font, fill=hex_to_rgb(BRAND_DARK))
    y += 42
    y = draw_wrapped(draw, issue, (50, y), body_bold, hex_to_rgb(BRAND_DARK), 980, 8)
    y += 26

    col_gap = 24
    col_w = (980 - col_gap) // 2
    card_h = 205
    left = (50, y, 50+col_w, y+card_h)
    right = (50+col_w+col_gap, y, 1030, y+card_h)

    draw.rounded_rectangle(left, radius=18, fill=hex_to_rgb(BRAND_WHITE))
    draw.rounded_rectangle(right, radius=18, fill=hex_to_rgb(BRAND_WHITE))
    draw.rectangle((left[0], left[1], left[0]+8, left[3]), fill=hex_to_rgb(BRAND_YELLOW))
    draw.rectangle((right[0], right[1], right[0]+8, right[3]), fill=hex_to_rgb(BRAND_MID))

    draw.text((left[0]+24, left[1]+18), "PASŪTĪTĀJA PRASĪBA", font=small_font, fill=hex_to_rgb(BRAND_DARK))
    draw_wrapped(draw, requirement, (left[0]+24, left[1]+55), body_font, hex_to_rgb(BRAND_DARK), col_w-48, 6)

    draw.text((right[0]+24, right[1]+18), "PROJEKTĀ", font=small_font, fill=hex_to_rgb(BRAND_DARK))
    draw_wrapped(draw, project, (right[0]+24, right[1]+55), body_font, hex_to_rgb(BRAND_DARK), col_w-48, 6)

    y += card_h + 30

    draw.text((50, y), "KĀPĒC TAS IR SVARĪGI?", font=label_font, fill=hex_to_rgb(BRAND_DARK))
    y += 42
    draw_wrapped(draw, impact, (50, y), body_font, hex_to_rgb(BRAND_DARK), 980, 7)

    draw.rectangle((0, H-92, W, H), fill=hex_to_rgb(BRAND_DARK))
    draw.text((50, H-62), footer, font=footer_font, fill=hex_to_rgb(BRAND_WHITE))

    if logo is not None:
        lg = logo.convert("RGBA")
        ratio = min(210 / lg.width, 46 / lg.height)
        lg = lg.resize((max(1, int(lg.width*ratio)), max(1, int(lg.height*ratio))), Image.LANCZOS)
        img.paste(lg.convert("RGB"), (W-lg.width-48, H-lg.height-22))

    return img

st.title("KywaTrace Case Example Generator")
st.caption("Anonimizē rasējuma fragmentu, izcel neatbilstību un eksportē vienotā KywaTrace LinkedIn noformējumā.")

with st.sidebar:
    st.header("1. Ievade")
    uploaded = st.file_uploader("Rasējuma fragments", type=["png", "jpg", "jpeg"])
    logo_file = st.file_uploader("KywaTrace logo (neobligāti)", type=["png", "jpg", "jpeg"])
    audience = st.selectbox("Auditorija", ["Attīstītājs", "Projektētājs", "Būvnieks"])

    st.header("2. Teksts")
    issue = st.text_area("Konstatēts", "Neatbilstība starp projektēto risinājumu un Design Brief prasībām.")
    requirement = st.text_area("Pasūtītāja prasība", "Gatavās grīdas līmeņiem jābūt vienā līnijā.")
    project = st.text_area("Projektā", "Terases grīdas līmenis paredzēts -20 mm.")
    impact = st.text_area("Kāpēc tas ir svarīgi?", "Savlaicīga neatbilstības identificēšana pirms tendera samazina izmaiņu, RFI, kavējumu un papildu izmaksu risku.")
    footer = st.text_input("Footer", "Anonimizēts ilustratīvs piemērs no KywaTrace audita")

if uploaded:
    source = Image.open(uploaded).convert("RGB")
    st.subheader("A. Apgriešana")
    c1, c2, c3, c4 = st.columns(4)
    crop_l = c1.slider("Kreisā mala %", 0, 90, 0)
    crop_t = c2.slider("Augšējā mala %", 0, 90, 0)
    crop_r = c3.slider("Labā mala %", 10, 100, 100)
    crop_b = c4.slider("Apakšējā mala %", 10, 100, 100)
    cropped = crop_by_pct(source, crop_l, crop_t, crop_r, crop_b)

    st.subheader("B. Anonimizācija")
    redact_count = st.slider("Aizsedzamo zonu skaits", 0, 5, 1)
    redact_mode = st.radio("Anonimizācijas veids", ["Blackout", "Blur"], horizontal=True)
    working = cropped.copy()

    for i in range(redact_count):
        st.markdown(f"**Zona {i+1}**")
        a, b, c, d = st.columns(4)
        l = a.slider(f"Kreisā % {i+1}", 0, 100, 0, key=f"rl{i}")
        t = b.slider(f"Augša % {i+1}", 0, 100, 0, key=f"rt{i}")
        r = c.slider(f"Labā % {i+1}", 0, 100, 20, key=f"rr{i}")
        bo = d.slider(f"Apakša % {i+1}", 0, 100, 15, key=f"rb{i}")
        working = redact(working, pct_box(working.width, working.height, l, t, r, bo), redact_mode)

    st.subheader("C. Problēmas izcelšana")
    h1, h2, h3, h4 = st.columns(4)
    hl = h1.slider("Kreisā %", 0, 100, 35, key="hl")
    ht = h2.slider("Augša %", 0, 100, 55, key="ht")
    hr = h3.slider("Labā %", 0, 100, 65, key="hr")
    hb = h4.slider("Apakša %", 0, 100, 85, key="hb")
    highlighted = add_highlight(
        working,
        pct_box(working.width, working.height, hl, ht, hr, hb),
        thickness=max(4, int(working.width * 0.008)),
    )

    logo = Image.open(logo_file).convert("RGBA") if logo_file else None
    card = build_card(highlighted, audience, issue, requirement, project, impact, footer, logo)

    left, right = st.columns([1, 1])
    with left:
        st.markdown("**Apstrādātais fragments**")
        st.image(highlighted, use_container_width=True)
    with right:
        st.markdown("**LinkedIn vizuāļa preview**")
        st.image(card, use_container_width=True)

    buf = io.BytesIO()
    card.save(buf, format="PNG")
    st.download_button(
        "Lejupielādēt PNG",
        data=buf.getvalue(),
        file_name="kywatrace_case_example.png",
        mime="image/png",
        use_container_width=True,
    )

    st.info("Pirms publicēšanas pārbaudi, vai attēlā nav palicis projekta nosaukums, dokumenta numurs, uzņēmuma identitāte vai cita atpazīstama informācija.")
else:
    st.warning("Augšupielādē rasējuma fragmentu, lai sāktu.")