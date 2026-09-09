import base64
import hashlib
import io
import zlib

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

# Compact 1-bit copy of the supplied KywaTrace logo.
# Keeping it embedded makes the exported PNG independent of external files/URLs.
LOGO_SIZE = (196, 131)
LOGO_B85 = (
    "c-rljO>Wyp6vszk0cK&8S#}X*+7r}hH!)y%&=d3uHVSktKpP7vN;)*GT%nu#DnqD1vk;;yP-$v!U&6o{0|ck<V}=x|wijpx3zX(JAMgL>|DNT&ygGS&3ST|M4{gqmKMsI1{HP89Xnqt2pw9TA@+q(>hoH<96bXl*^c1KWhoG_v=#=8GDxH9K#>duAk_m80LC4x>vk8FI(y_&vnt(cEAu5kTBCr}#3c(j<3<~2C3c*`F1}euv3?Zf_0^9@tFa-MB2Q(dz#px~?0s4x^;sNb!1ZV>sMb@b?$oNKZw+V15t)junR&7G(gosDs&S)+TPQvwyw9w#0pf50Y>OAuYDY2Nlf-ZcT#2UveS!A5{dl4csx_kg@5h5~LGk_}*A~KvFfD$1h1F{};xVe%QD5Fk$&|)|!gw8-2HtB&w0Vsr`LK(%Z2Y`-6AyyU!v$8sLEEra9$;EjD_6rO$=UnYj31@{taf-4qVvMyxC7eeF>SyKSeV`J~Jcy}iTe(40!gmE?D%w`=9F@pIF%@kqF2Omx5X4k<7JY?~7lM>HZ7WWsQCbMRE1O2VOZ)JNTpRQ?xSgiZi6CtPN5ykKS}3eQ7%D1+5zjyvYS8N844D+FwlG6fvaQs)iIotPBb3hYJXS);6+-DHN3jw@cF_-^`@~8BABBW|6)Qn*kRk~kE>?ovImI4&Vypy+9n^*{8Y=<%V%9@t4Njz_NhMi)4^C`xwv$TQ;kz8%j<Z{-um<11kNWQP^*<QBsQ<vqI+IpTr#{8&#jep&H|f;VRfS;J11y^q|Ml&IE@5DLP`b0M9)lub&_2&<VDRc|cdXmgmnDHzK!(5>cdD^~%5|3k0bN734^{+jvhEoJv~~|F0Y@6(^FyG!BeM@&*y>vQ0KN)f=2xD8@lOm~?1AqDptThN?IO4sKn@o-+I_hX)Ew<y<c`2O0i{8}F*RBNa|Q;^2jJbs_1^*O-4E9q1bC<epeF2+YXkFK0EDPC{@xkDab5@TGDFZNzoGguxDj9txCru)_7Ql&pyG=PpvGYYW;X(<&3oViSWQ3`K^efVC2)$s@Zk))w2nX{K>Hx50;pOYff#NI)+3NbaF&&=5%1Ol{JIC&f)PlmVEftjpZLe*f1zc7yIk(>^7ZEZ{hQCW06#9uZ*vBh^Ar0vzant7g?SG?ESr<f+x!;<+h4zbSllgczlmVk1O2dgSlqr0;PG)wU?<>16T#)><Les1pZD8aG(9wB5x`GN2JaXEG^+rX-O1)1f*OJO*g$jp%J)(IE1<2+!@@<t0`KyxYEu)KFPrl9C?&f$J=p9g@zXzLx0!&y0OMrif&"
)


def hex_to_rgb(value: str):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def load_brand_logo():
    packed = base64.b85decode(LOGO_B85.encode("ascii"))
    raw = zlib.decompress(packed)
    return Image.frombytes("1", LOGO_SIZE, raw).convert("RGB")


def scale_fragment(fragment, max_w=1400, max_h=900, min_w=900):
    """Resize the example without letterboxing and keep its original aspect ratio."""
    fragment = fragment.copy().convert("RGB")

    scale = min(max_w / fragment.width, max_h / fragment.height, 1.0)

    # Small screenshots can be enlarged a little for a useful LinkedIn export,
    # but never more than 1.6x to avoid excessive softening.
    if fragment.width < min_w and scale == 1.0:
        scale = min(min_w / fragment.width, max_h / fragment.height, 1.6)

    new_w = max(1, int(round(fragment.width * scale)))
    new_h = max(1, int(round(fragment.height * scale)))

    if (new_w, new_h) != fragment.size:
        fragment = fragment.resize((new_w, new_h), Image.LANCZOS)

    return fragment


def build_card(fragment):
    """Create a close-fitting rectangular KywaTrace export around the example."""
    content = scale_fragment(fragment)

    page_margin = 24
    outer_inset = 18
    inner_pad = 18
    footer_h = 150

    inner_w = content.width + 2 * inner_pad
    inner_h = content.height + 2 * inner_pad + footer_h

    W = inner_w + 2 * (page_margin + outer_inset)
    H = inner_h + 2 * (page_margin + outer_inset)

    img = Image.new("RGB", (W, H), hex_to_rgb(BRAND_LIGHT))
    draw = ImageDraw.Draw(img)

    outer = (
        page_margin,
        page_margin,
        W - page_margin - 1,
        H - page_margin - 1,
    )
    inner = (
        page_margin + outer_inset,
        page_margin + outer_inset,
        W - page_margin - outer_inset - 1,
        H - page_margin - outer_inset - 1,
    )

    draw.rounded_rectangle(
        outer,
        radius=30,
        fill=hex_to_rgb(BRAND_WHITE),
        outline=hex_to_rgb(BRAND_DARK_BLUE),
        width=9,
    )

    draw.rounded_rectangle(
        inner,
        radius=22,
        outline=hex_to_rgb(BRAND_DARK_BLUE_2),
        width=2,
    )

    content_x = inner[0] + inner_pad
    content_y = inner[1] + inner_pad
    img.paste(content, (content_x, content_y))

    footer_top = content_y + content.height
    footer_bottom = inner[3] - inner_pad
    footer_center_y = (footer_top + footer_bottom) // 2

    logo = load_brand_logo()
    max_logo_h = min(108, footer_h - 28)
    logo_scale = min(210 / logo.width, max_logo_h / logo.height)
    logo = logo.resize(
        (
            max(1, int(round(logo.width * logo_scale))),
            max(1, int(round(logo.height * logo_scale))),
        ),
        Image.LANCZOS,
    )

    logo_x = (W - logo.width) // 2
    logo_y = footer_center_y - logo.height // 2 + 2

    line_y = footer_center_y
    line_gap = 34
    line_left_start = content_x + 14
    line_left_end = logo_x - line_gap
    line_right_start = logo_x + logo.width + line_gap
    line_right_end = content_x + content.width - 14

    if line_left_end > line_left_start:
        draw.line(
            (line_left_start, line_y, line_left_end, line_y),
            fill=hex_to_rgb(BRAND_DARK_BLUE_2),
            width=3,
        )

    if line_right_end > line_right_start:
        draw.line(
            (line_right_start, line_y, line_right_end, line_y),
            fill=hex_to_rgb(BRAND_DARK_BLUE_2),
            width=3,
        )

    img.paste(logo, (logo_x, logo_y))
    return img


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
    "Izveido blur zonas un eksportē anonimizētu piemēru ar saturam pielāgotu "
    "KywaTrace rāmi un logo."
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
        help=(
            "Mazāks skaitlis ļauj nedaudz nojaust saturu zem blur; "
            "lielāks to noslēpj daudz izteiktāk."
        ),
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

editor_background = editor_background.resize(
    (editor_display_w, editor_display_h),
    Image.LANCZOS,
)

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
scaled_rect = scale_zone_from_editor(
    rect,
    editor_scale,
    source.width,
    source.height,
)

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
        duplicate["left"] = min(
            source.width - duplicate["width"],
            duplicate["left"] + 20,
        )
        duplicate["top"] = min(
            source.height - duplicate["height"],
            duplicate["top"] + 20,
        )
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
