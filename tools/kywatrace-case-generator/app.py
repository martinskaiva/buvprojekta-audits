import io
import hashlib

import streamlit as st
from PIL import Image, ImageDraw, ImageFilter
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
        .block-container {padding-top: 1rem; padding-bottom: 1.25rem;}
    </style>
    """,
    unsafe_allow_html=True,
)

BRAND_DARK_BLUE = "#032766"
BRAND_DARK_BLUE_2 = "#0A3A84"
BRAND_LIGHT = "#F6F6F2"
BRAND_WHITE = "#FFFFFF"
BRAND_YELLOW = "#F2D400"


def hex_to_rgb(value: str):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


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


def build_card(fragment):
    W, H = 1080, 1080
    img = Image.new("RGB", (W, H), hex_to_rgb(BRAND_LIGHT))
    draw = ImageDraw.Draw(img)

    outer = (34, 34, 1046, 1046)
    inner = (58, 58, 1022, 1022)

    draw.rounded_rectangle(
        outer,
        radius=34,
        fill=hex_to_rgb(BRAND_WHITE),
        outline=hex_to_rgb(BRAND_DARK_BLUE),
        width=10,
    )

    draw.rounded_rectangle(
        inner,
        radius=26,
        outline=hex_to_rgb(BRAND_DARK_BLUE_2),
        width=2,
    )

    fitted = fit_image(fragment, inner[2] - inner[0] - 24, inner[3] - inner[1] - 24)
    img.paste(fitted, (inner[0] + 12, inner[1] + 12))

    return img


def scale_zone_for_editor(zone, editor_scale):
    return {
        "left": int(zone["left"] * editor_scale),
        "top": int(zone["top"] * editor_scale),
        "width": int(zone["width"] * editor_scale),
        "height": int(zone["height"] * editor_scale),
    }


def scale_zone_from_editor(zone, editor_scale, source_w, source_h):
    left = int(zone["left"] / editor_scale)
    top = int(zone["top"] / editor_scale)
    width = int(zone["width"] / editor_scale)
    height = int(zone["height"] / editor_scale)

    left = max(0, min(left, source_w - 1))
    top = max(0, min(top, source_h - 1))
    width = max(1, min(width, source_w - left))
    height = max(1, min(height, source_h - top))

    return {
        "left": left,
        "top": top,
        "width": width,
        "height": height,
    }


st.title("KywaTrace Case Example Generator")
st.caption(
    "Izveido blur zonas uz rasējuma un eksportē tikai anonimizēto attēlu ar tumši zilu rāmi."
)

with st.sidebar:
    st.header("Ievade")
    uploaded = st.file_uploader("Rasējuma fragments", type=["png", "jpg", "jpeg"])

    st.divider()
    st.header("Blur")
    blur_radius = st.slider(
        "Blur stiprums",
        min_value=2,
        max_value=80,
        value=22,
        step=2,
        help="Mazāks skaitlis ļauj nedaudz nojaust saturu zem blur; lielāks to noslēpj daudz izteiktāk.",
    )

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

# Bigger editor area
EDITOR_MAX_W = 1600
EDITOR_MAX_H = 950
editor_scale = min(EDITOR_MAX_W / source.width, EDITOR_MAX_H / source.height, 2.2)

editor_display_w = max(1, int(source.width * editor_scale))
editor_display_h = max(1, int(source.height * editor_scale))

st.subheader("1. Blur zonas")

choice_labels = ["+ Jauna blur zona"] + [f"Zona {i + 1}" for i in range(len(zones))]
current_choice = st.selectbox("Rediģējamā zona", choice_labels, key="blur_zone_choice")
edit_index = -1 if current_choice.startswith("+") else int(current_choice.split()[-1]) - 1

editor_background = apply_blur_zones(
    source,
    zones,
    blur_radius,
    skip_index=edit_index if edit_index >= 0 else None,
)

editor_background = editor_background.resize((editor_display_w, editor_display_h), Image.LANCZOS)

if edit_index >= 0:
    z = scale_zone_for_editor(zones[edit_index], editor_scale)
    default_coords = (
        int(z["left"]),
        int(z["left"] + z["width"]),
        int(z["top"]),
        int(z["top"] + z["height"]),
    )
else:
    default_coords = None

st.caption(
    "Izvēlies esošu zonu vai veido jaunu. Var koriģēt vienu izvēlēto rāmi vienlaikus."
)

rect = st_cropper(
    img_file=editor_background,
    realtime_update=True,
    default_coords=default_coords,
    box_color=BRAND_YELLOW,
    aspect_ratio=None,
    return_type="box",
    key=f"blur_cropper_{file_id}_{edit_index}",
    stroke_width=4,
)

rect = {k: int(v) for k, v in rect.items()}
scaled_rect = scale_zone_from_editor(rect, editor_scale, source.width, source.height)

c1, c2, c3 = st.columns([1, 1, 1])

with c1:
    if edit_index < 0:
        if st.button("Pievienot blur zonu", use_container_width=True):
            st.session_state.blur_zones.append(scaled_rect)
            st.rerun()
    else:
        if st.button("Saglabāt izmaiņas", use_container_width=True):
            st.session_state.blur_zones[edit_index] = scaled_rect
            st.rerun()

with c2:
    if edit_index >= 0 and st.button("Dublēt zonu", use_container_width=True):
        duplicate = dict(scaled_rect)
        duplicate["left"] = min(source.width - duplicate["width"], duplicate["left"] + 20)
        duplicate["top"] = min(source.height - duplicate["height"], duplicate["top"] + 20)
        st.session_state.blur_zones.append(duplicate)
        st.rerun()

with c3:
    if edit_index >= 0 and st.button("Dzēst zonu", use_container_width=True):
        del st.session_state.blur_zones[edit_index]
        st.rerun()

st.caption(
    f"Saglabātas blur zonas: {len(zones)}"
    if zones
    else "Neviena blur zona vēl nav saglabāta."
)

blurred = apply_blur_zones(source, zones, blur_radius)
card = build_card(blurred)

st.subheader("2. Rezultāts")
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
    "Pirms publicēšanas pārbaudi gala PNG: blur palīdz anonimizēt, "
    "bet pats par sevi negarantē, ka visi projekta identifikatori ir paslēpti."
)
