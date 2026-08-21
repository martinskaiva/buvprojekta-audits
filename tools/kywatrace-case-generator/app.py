import io
import hashlib

import streamlit as st
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from streamlit_cropper import st_cropper

st.set_page_config(
    page_title="KywaTrace Case Example Generator",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
        header[data-testid="stHeader"] {display: none;}
        div[data-testid="stToolbar"] {display: none;}
        #MainMenu {visibility: hidden;}
        footer {visibility: hidden;}
        .block-container {padding-top: 1.2rem; padding-bottom: 2rem;}
    </style>
    """,
    unsafe_allow_html=True,
)

BRAND_YELLOW = "#F2D400"
BRAND_DARK = "#111111"
BRAND_LIGHT = "#F6F6F2"
BRAND_MID = "#D8D8D3"
BRAND_WHITE = "#FFFFFF"


def hex_to_rgb(value: str):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


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


def apply_blur_zones(source, zones, blur_radius, skip_index=None):
    out = source.copy()
    for idx, zone in enumerate(zones):
        if skip_index is not None and idx == skip_index:
            continue
        left = int(zone["left"])
        top = int(zone["top"])
        width = int(zone["width"])
        height = int(zone["height"])
        x1 = max(0, left)
        y1 = max(0, top)
        x2 = min(out.width, left + width)
        y2 = min(out.height, top + height)
        if x2 <= x1 or y2 <= y1:
            continue
        region = out.crop((x1, y1, x2, y2))
        region = region.filter(ImageFilter.GaussianBlur(radius=blur_radius))
        out.paste(region, (x1, y1, x2, y2))
    return out


def build_card(fragment, audience, issue, requirement, project, impact, footer, logo=None):
    W, H = 1080, 1080
    img = Image.new("RGB", (W, H), hex_to_rgb(BRAND_LIGHT))
    draw = ImageDraw.Draw(img)

    title_font = get_font(34, True)
    small_font = get_font(20, False)
    label_font = get_font(22, True)
    body_font = get_font(25, False)
    body_bold = get_font(25, True)
    footer_font = get_font(18, False)

    panel_x1, panel_y1, panel_x2, panel_y2 = 40, 38, 1040, 610
    draw.rounded_rectangle((panel_x1, panel_y1, panel_x2, panel_y2), radius=24, fill=hex_to_rgb(BRAND_WHITE))
    fitted = fit_image(fragment, panel_x2 - panel_x1 - 24, panel_y2 - panel_y1 - 24)
    img.paste(fitted, (panel_x1 + 12, panel_y1 + 12))

    if logo is not None:
        lg = logo.convert("RGBA")
        ratio = min(170 / lg.width, 84 / lg.height)
        lg = lg.resize((max(1, int(lg.width * ratio)), max(1, int(lg.height * ratio))), Image.LANCZOS)

        logo_pad_x = 14
        logo_pad_y = 10
        logo_bg_w = lg.width + logo_pad_x * 2
        logo_bg_h = lg.height + logo_pad_y * 2
        logo_x = panel_x1 + 34
        logo_y = panel_y1 + 155

        draw.rounded_rectangle(
            (logo_x, logo_y, logo_x + logo_bg_w, logo_y + logo_bg_h),
            radius=14,
            fill=hex_to_rgb(BRAND_WHITE),
        )
        img.paste(lg.convert("RGB"), (logo_x + logo_pad_x, logo_y + logo_pad_y))
    else:
        draw.text((panel_x1 + 36, panel_y1 + 36), "KywaTrace", font=title_font, fill=hex_to_rgb(BRAND_DARK))

    y = 642
    draw.text((40, y), "KONSTATĒTS", font=label_font, fill=hex_to_rgb(BRAND_DARK))
    y += 34
    y = draw_wrapped(draw, issue, (40, y), body_bold, hex_to_rgb(BRAND_DARK), 1000, 5)
    y += 16

    col_gap = 22
    col_w = (1000 - col_gap) // 2
    card_h = 132
    left = (40, y, 40 + col_w, y + card_h)
    right = (40 + col_w + col_gap, y, 1040, y + card_h)

    draw.rounded_rectangle(left, radius=18, fill=hex_to_rgb(BRAND_WHITE))
    draw.rounded_rectangle(right, radius=18, fill=hex_to_rgb(BRAND_WHITE))
    draw.rectangle((left[0], left[1], left[0] + 8, left[3]), fill=hex_to_rgb(BRAND_YELLOW))
    draw.rectangle((right[0], right[1], right[0] + 8, right[3]), fill=hex_to_rgb(BRAND_MID))

    draw.text((left[0] + 24, left[1] + 15), "PASŪTĪTĀJA PRASĪBA", font=small_font, fill=hex_to_rgb(BRAND_DARK))
    draw_wrapped(draw, requirement, (left[0] + 24, left[1] + 44), body_font, hex_to_rgb(BRAND_DARK), col_w - 48, 4)

    draw.text((right[0] + 24, right[1] + 15), "PROJEKTĀ", font=small_font, fill=hex_to_rgb(BRAND_DARK))
    draw_wrapped(draw, project, (right[0] + 24, right[1] + 44), body_font, hex_to_rgb(BRAND_DARK), col_w - 48, 4)

    y += card_h + 18
    draw.text((40, y), "KĀPĒC TAS IR SVARĪGI?", font=label_font, fill=hex_to_rgb(BRAND_DARK))
    y += 33
    draw_wrapped(draw, impact, (40, y), body_font, hex_to_rgb(BRAND_DARK), 1000, 5)

    draw.rectangle((0, H - 74, W, H), fill=hex_to_rgb(BRAND_DARK))
    draw.text((40, H - 49), footer, font=footer_font, fill=hex_to_rgb(BRAND_WHITE))
    draw.text((W - 250, H - 51), audience.upper(), font=small_font, fill=hex_to_rgb(BRAND_YELLOW))

    return img


st.title("KywaTrace Case Example Generator")
st.caption("Blur zonu velc un koriģē tieši uz rasējuma ar peli. Saglabā vairākas zonas un pēc vajadzības atgriezies pie jebkuras no tām.")

with st.sidebar:
    st.header("Ievade")
    uploaded = st.file_uploader("Rasējuma fragments", type=["png", "jpg", "jpeg"])
    logo_file = st.file_uploader("KywaTrace logo (neobligāti)", type=["png", "jpg", "jpeg"])
    audience = st.selectbox("Auditorija", ["Attīstītājs", "Projektētājs", "Būvnieks"])

    st.divider()
    st.header("Blur")
    blur_radius = st.slider(
        "Blur stiprums",
        min_value=2,
        max_value=80,
        value=22,
        step=2,
        help="Mazāks skaitlis ļauj nojaust saturu zem blur; lielāks to noslēpj daudz izteiktāk.",
    )

    st.divider()
    st.header("Teksts")
    issue = st.text_area("Konstatēts", "Neatbilstība starp projektēto risinājumu un Design Brief prasībām.")
    requirement = st.text_area("Pasūtītāja prasība", "Gatavās grīdas līmeņiem jābūt vienā līnijā.")
    project = st.text_area("Projektā", "Terases grīdas līmenis paredzēts -20 mm.")
    impact = st.text_area(
        "Kāpēc tas ir svarīgi?",
        "Savlaicīga neatbilstības identificēšana pirms tendera samazina izmaiņu, RFI, kavējumu un papildu izmaksu risku.",
    )
    footer = st.text_input("Footer", "Anonimizēts ilustratīvs piemērs no KywaTrace audita")

if not uploaded:
    st.info("Augšupielādē rasējuma fragmentu kreisajā pusē.")
    st.stop()

raw_bytes = uploaded.getvalue()
file_id = hashlib.sha1(raw_bytes).hexdigest()
source = Image.open(io.BytesIO(raw_bytes)).convert("RGB")

if st.session_state.get("blur_file_id") != file_id:
    st.session_state.blur_file_id = file_id
    st.session_state.blur_zones = []

zones = st.session_state.blur_zones

st.subheader("1. Blur zonas")
choices = ["+ Jauna blur zona"] + [f"Zona {i + 1}" for i in range(len(zones))]
current_choice = st.selectbox("Rediģējamā zona", choices, key="blur_zone_choice")
edit_index = -1 if current_choice.startswith("+") else int(current_choice.split()[-1]) - 1

editor_background = apply_blur_zones(source, zones, blur_radius, skip_index=edit_index if edit_index >= 0 else None)

if edit_index >= 0:
    z = zones[edit_index]
    default_coords = (
        int(z["left"]),
        int(z["left"] + z["width"]),
        int(z["top"]),
        int(z["top"] + z["height"]),
    )
else:
    default_coords = None

st.caption("Velc dzeltenā rāmja malas vai stūrus, lai mainītu izmēru. Satver rāmja iekšpusi, lai to pārvietotu.")

rect = st_cropper(
    img_file=editor_background,
    realtime_update=True,
    default_coords=default_coords,
    box_color=BRAND_YELLOW,
    aspect_ratio=None,
    return_type="box",
    key=f"blur_cropper_{file_id}_{edit_index}",
    stroke_width=3,
)
rect = {k: int(v) for k, v in rect.items()}

c1, c2, c3 = st.columns([1, 1, 1])
with c1:
    if edit_index < 0:
        if st.button("Pievienot blur zonu", use_container_width=True):
            st.session_state.blur_zones.append(rect)
            st.rerun()
    else:
        if st.button("Saglabāt izmaiņas", use_container_width=True):
            st.session_state.blur_zones[edit_index] = rect
            st.rerun()
with c2:
    if edit_index >= 0 and st.button("Dublēt zonu", use_container_width=True):
        duplicate = dict(rect)
        duplicate["left"] = min(source.width - duplicate["width"], duplicate["left"] + 20)
        duplicate["top"] = min(source.height - duplicate["height"], duplicate["top"] + 20)
        st.session_state.blur_zones.append(duplicate)
        st.rerun()
with c3:
    if edit_index >= 0 and st.button("Dzēst zonu", use_container_width=True):
        del st.session_state.blur_zones[edit_index]
        st.rerun()

st.caption(f"Saglabātas blur zonas: {len(zones)}" if zones else "Neviena blur zona vēl nav saglabāta.")

blurred = apply_blur_zones(source, zones, blur_radius)
logo = Image.open(logo_file).convert("RGBA") if logo_file else None
card = build_card(blurred, audience, issue, requirement, project, impact, footer, logo)

st.subheader("2. Rezultāts")
left, right = st.columns([1, 1])
with left:
    st.markdown("**Anonimizētais rasējuma fragments**")
    st.image(blurred, use_container_width=True)
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

st.warning("Pirms publicēšanas pārbaudi gala PNG: blur palīdz anonimizēt, bet pats par sevi negarantē, ka visi projekta identifikatori ir paslēpti.")