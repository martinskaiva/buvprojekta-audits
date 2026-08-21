import base64
import io

import numpy as np
import streamlit as st
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import streamlit_drawable_canvas as sdc

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
        .block-container {padding-top: 1.4rem; padding-bottom: 2rem;}
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


def pil_to_data_uri(image: Image.Image) -> str:
    image = image.copy()
    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGBA")
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    encoded = base64.b64encode(buf.getvalue()).decode("utf-8")
    return f"data:image/png;base64,{encoded}"


def st_canvas_cloud(
    fill_color="#eee",
    stroke_width=20,
    stroke_color="black",
    background_color="",
    background_image=None,
    update_streamlit=True,
    height=400,
    width=600,
    drawing_mode="freedraw",
    initial_drawing=None,
    display_toolbar=True,
    point_display_radius=3,
    key=None,
):
    """Cloud-safe wrapper around streamlit-drawable-canvas using an embedded background image."""
    background_image_url = None
    if background_image is not None:
        resized = background_image.resize((width, height), Image.LANCZOS)
        background_image_url = pil_to_data_uri(resized)
        background_color = ""

    initial_drawing = {"version": "4.4.0"} if initial_drawing is None else initial_drawing
    initial_drawing["background"] = background_color

    component_value = sdc._component_func(
        fillColor=fill_color,
        strokeWidth=stroke_width,
        strokeColor=stroke_color,
        backgroundColor=background_color,
        backgroundImageURL=background_image_url,
        realtimeUpdateStreamlit=update_streamlit and (drawing_mode != "polygon"),
        canvasHeight=height,
        canvasWidth=width,
        drawingMode=drawing_mode,
        initialDrawing=initial_drawing,
        displayToolbar=display_toolbar,
        displayRadius=point_display_radius,
        key=key,
        default=None,
    )

    if component_value is None:
        return sdc.CanvasResult()

    return sdc.CanvasResult(
        np.asarray(sdc._data_url_to_image(component_value["data"])),
        component_value["raw"],
    )


def apply_blur_zones(source, objects, canvas_w, canvas_h, blur_radius):
    out = source.copy()
    if not objects:
        return out

    scale_x = source.width / canvas_w
    scale_y = source.height / canvas_h

    for obj in objects:
        if obj.get("type") != "rect":
            continue

        left = float(obj.get("left", 0))
        top = float(obj.get("top", 0))
        width = float(obj.get("width", 0)) * float(obj.get("scaleX", 1))
        height = float(obj.get("height", 0)) * float(obj.get("scaleY", 1))

        x1 = max(0, int(left * scale_x))
        y1 = max(0, int(top * scale_y))
        x2 = min(source.width, int((left + width) * scale_x))
        y2 = min(source.height, int((top + height) * scale_y))

        if x2 <= x1 or y2 <= y1:
            continue

        region = out.crop((x1, y1, x2, y2))
        region = region.filter(ImageFilter.GaussianBlur(radius=blur_radius))
        out.paste(region, (x1, y1, x2, y2))

    return out


def build_card(fragment, audience, issue, requirement, project, impact, footer, logo=None):
    W, H = 1080, 1350
    img = Image.new("RGB", (W, H), hex_to_rgb(BRAND_LIGHT))
    draw = ImageDraw.Draw(img)

    title_font = get_font(42, True)
    small_font = get_font(22, False)
    label_font = get_font(24, True)
    body_font = get_font(28, False)
    body_bold = get_font(28, True)
    footer_font = get_font(19, False)

    panel_x1, panel_y1, panel_x2, panel_y2 = 50, 55, 1030, 650
    draw.rounded_rectangle(
        (panel_x1, panel_y1, panel_x2, panel_y2),
        radius=20,
        fill=hex_to_rgb(BRAND_WHITE),
    )
    fitted = fit_image(fragment, panel_x2 - panel_x1 - 30, panel_y2 - panel_y1 - 30)
    img.paste(fitted, (panel_x1 + 15, panel_y1 + 15))

    y = 695
    draw.text((50, y), "KONSTATĒTS", font=label_font, fill=hex_to_rgb(BRAND_DARK))
    y += 42
    y = draw_wrapped(draw, issue, (50, y), body_bold, hex_to_rgb(BRAND_DARK), 980, 8)
    y += 26

    col_gap = 24
    col_w = (980 - col_gap) // 2
    card_h = 205
    left = (50, y, 50 + col_w, y + card_h)
    right = (50 + col_w + col_gap, y, 1030, y + card_h)

    draw.rounded_rectangle(left, radius=18, fill=hex_to_rgb(BRAND_WHITE))
    draw.rounded_rectangle(right, radius=18, fill=hex_to_rgb(BRAND_WHITE))
    draw.rectangle((left[0], left[1], left[0] + 8, left[3]), fill=hex_to_rgb(BRAND_YELLOW))
    draw.rectangle((right[0], right[1], right[0] + 8, right[3]), fill=hex_to_rgb(BRAND_MID))

    draw.text((left[0] + 24, left[1] + 18), "PASŪTĪTĀJA PRASĪBA", font=small_font, fill=hex_to_rgb(BRAND_DARK))
    draw_wrapped(draw, requirement, (left[0] + 24, left[1] + 55), body_font, hex_to_rgb(BRAND_DARK), col_w - 48, 6)

    draw.text((right[0] + 24, right[1] + 18), "PROJEKTĀ", font=small_font, fill=hex_to_rgb(BRAND_DARK))
    draw_wrapped(draw, project, (right[0] + 24, right[1] + 55), body_font, hex_to_rgb(BRAND_DARK), col_w - 48, 6)

    y += card_h + 30
    draw.text((50, y), "KĀPĒC TAS IR SVARĪGI?", font=label_font, fill=hex_to_rgb(BRAND_DARK))
    y += 42
    draw_wrapped(draw, impact, (50, y), body_font, hex_to_rgb(BRAND_DARK), 980, 7)

    draw.rectangle((0, H - 92, W, H), fill=hex_to_rgb(BRAND_DARK))
    draw.text((50, H - 62), footer, font=footer_font, fill=hex_to_rgb(BRAND_WHITE))
    draw.text((W - 280, H - 66), audience.upper(), font=small_font, fill=hex_to_rgb(BRAND_YELLOW))

    if logo is not None:
        lg = logo.convert("RGBA")
        ratio = min(210 / lg.width, 46 / lg.height)
        lg = lg.resize((max(1, int(lg.width * ratio)), max(1, int(lg.height * ratio))), Image.LANCZOS)
        img.paste(lg.convert("RGB"), (W - lg.width - 48, 20))
    else:
        draw.text((50, 18), "KywaTrace", font=title_font, fill=hex_to_rgb(BRAND_DARK))

    return img


st.title("KywaTrace Case Example Generator")
st.caption(
    "Uzzīmē blur zonas tieši uz rasējuma. Vispirms uzzīmē taisnstūri, pēc tam pārslēdzies uz koriģēšanas režīmu, "
    "lai to pārvietotu vai pavilktu malas."
)

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
        help="Mazāks skaitlis ļauj nojaust, kas atrodas zem blur; lielāks gandrīz pilnībā noslēpj saturu.",
    )
    edit_mode = st.radio(
        "Darbība ar peli",
        ["Pievienot blur zonu", "Koriģēt esošu zonu"],
        help="Pievieno vairākus taisnstūrus. Koriģēšanas režīmā klikšķini uz jau uzzīmētās zonas un velc to vai tās malas.",
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

source = Image.open(uploaded).convert("RGB")

MAX_CANVAS_W = 1100
MAX_CANVAS_H = 720
scale = min(MAX_CANVAS_W / source.width, MAX_CANVAS_H / source.height, 1.0)
canvas_w = max(1, int(source.width * scale))
canvas_h = max(1, int(source.height * scale))
canvas_bg = source.resize((canvas_w, canvas_h), Image.LANCZOS)

st.subheader("1. Atzīmē anonimizējamās vietas")
st.caption(
    "Zīmē tieši uz rasējuma. Ja taisnstūris jāpielāgo, pārslēdzies uz koriģēšanas režīmu un pavelc tā malas vai pārvieto visu zonu."
)

canvas_mode = "rect" if edit_mode == "Pievienot blur zonu" else "transform"

canvas_result = st_canvas_cloud(
    fill_color="rgba(242, 212, 0, 0.16)",
    stroke_width=2,
    stroke_color=BRAND_YELLOW,
    background_color="#FFFFFF",
    background_image=canvas_bg,
    update_streamlit=True,
    height=canvas_h,
    width=canvas_w,
    drawing_mode=canvas_mode,
    display_toolbar=True,
    key="blur_canvas",
)

objects = []
if canvas_result.json_data and "objects" in canvas_result.json_data:
    objects = canvas_result.json_data["objects"]

blurred = apply_blur_zones(source, objects, canvas_w, canvas_h, blur_radius)
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

st.warning(
    "Pirms publicēšanas pārbaudi gala PNG: blur palīdz anonimizēt, bet pats par sevi negarantē, ka visi projekta identifikatori ir paslēpti."
)
