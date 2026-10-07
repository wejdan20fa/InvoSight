import base64
import json
import logging
from copy import deepcopy
from html import escape
from pathlib import Path
from textwrap import dedent
from datetime import datetime
from zoneinfo import ZoneInfo
from Database import connection
import streamlit as st
from report_generator import build_verification_report
from Dount_backend import extract_invoice

from Database import (
    dashboard_stats,
    list_invoices,
    save_invoice,
    InvoiceFilenameConflict,
    delete_invoice,
    delete_all_invoices,
    update_invoice_fields,
    render_database_page,
)
from ocr_evidence import run_ocr, extract_ocr_rates
from validation_rules import (
    validate_fields,
    changed_fields,
    recalculate_linked_fields,
    check_editable_money,
    public_status,
    public_reason,
)
logger = logging.getLogger(__name__)
def render_html(markup: str, **kwargs):
    """Render indented HTML/CSS as markup rather than Markdown code blocks."""
    return st.markdown(dedent(markup).strip(), **kwargs)
st.set_page_config(
    page_title="InvoSight",
    page_icon="📄",
    layout="wide",
    initial_sidebar_state="expanded"
)
BASE_DIR = Path(__file__).resolve().parent
ASSETS = BASE_DIR / "InvoSight_Assets"
ICONS = ASSETS
IMAGES = ASSETS
def asset_file(filename):
    for folder in (ASSETS, ASSETS / "icons", ASSETS / "images"):
        match = folder / filename
        if match.is_file():
            return match
    return None
def asset_data(filename):
    path = asset_file(filename)
    if not path:
        return ""
    mime = "image/png" if path.suffix.lower() == ".png" else "image/svg+xml"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"
SETTINGS_PATH = BASE_DIR / "invosight_preferences.json"
DEFAULT_PREFERENCES = {
    "workspace_name": "My Workspace",
    "landing_page": "Dashboard",
    "show_validation_notes": True,
    "history_limit": 25,
}
def read_preferences():
    try:
        stored = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        if isinstance(stored, dict):
            return {**DEFAULT_PREFERENCES, **{key: stored[key] for key in DEFAULT_PREFERENCES if key in stored}}
    except (OSError, ValueError):
        pass
    return DEFAULT_PREFERENCES.copy()
if "ui_preferences" not in st.session_state:
    st.session_state.ui_preferences = read_preferences()
def icon_html(filename, css_class="section-symbol"):
    uri = asset_data(filename)
    return f'<img class="{css_class}" src="{uri}" alt="">' if uri else ""
def image_base64(path):
    if not path.exists():
        return ""
    return base64.b64encode(path.read_bytes()).decode("utf-8")
logo_uri = asset_data("logo-full.png") or asset_data("logo.svg")
sidebar_bg_uri = asset_data("p-background2.png") or asset_data("sidebar_bg.png")
dashboard_bg_uri = asset_data("dashboard-background.png")
if "page" not in st.session_state:
    st.session_state.page = st.session_state.ui_preferences.get("landing_page", "Dashboard")
if "invoice_data" not in st.session_state:
    st.session_state.invoice_data = None
if "invoice_name" not in st.session_state:
    st.session_state.invoice_name = None
if "upload_version" not in st.session_state:
    st.session_state.upload_version = 0
def reset_invoice():
    """Replace Invoice clears all previous data and invalidates old OCR."""
    st.session_state.invoice_data = None
    st.session_state.invoice_name = None
    st.session_state.upload_version += 1
    st.session_state.saved_fields = {key: "" for key in FIELD_LABELS}
    st.session_state.original_fields = None
    st.session_state.extraction_complete = False
    st.session_state.ocr_lines = []
    st.session_state.ocr_status = ""
    st.session_state.discount_rate = None
    st.session_state.tax_rate = None
    st.session_state.line_items = None
    st.session_state.manual_tax_override = False
    st.session_state.edit_error = ""
    st.session_state.edit_note = ""
    st.session_state.pop("donut_raw_output", None)
    st.session_state.pop("ui_flash", None)
    st.session_state.edit_fields = False
    for key in FIELD_LABELS:
        st.session_state.pop(f"field_{key}", None)
        if "live_field_revision" in st.session_state:
            refresh_live_input(key)
def on_invoice_upload():
    uploaded = st.session_state.get(f"invoice_{st.session_state.upload_version}")
    if uploaded is not None:
        st.session_state.invoice_data = uploaded.getvalue()
        st.session_state.invoice_name = uploaded.name

action_icons_css = ""
for control, image_name in (("save_database_btn", "database.svg"),
                            ("download_json_btn", "file-json.svg"),
                            ("download_report_btn", "download.svg"),
                            ("reset_field_changes_btn", "refresh.svg"),
                            ("save_field_changes_btn", "save.svg")):
    uri = asset_data(image_name)
    if uri:
        action_icons_css += (f'.st-key-{control} button {{padding-left:39px !important;'
                             f'background-image:url("{uri}") !important;'
                             'background-repeat:no-repeat !important;background-size:21px 21px !important;'
                             'background-position:14px center !important;}')
background_layer = (f'url("{dashboard_bg_uri}") center top / 100% auto no-repeat,' if dashboard_bg_uri else "")
NAV_ICON_PATHS = {
    "Dashboard": '<path d="m3 10 9-7 9 7v10a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1z"/>',
    "History": '<path d="M3 12a9 9 0 1 0 3-6.7M3 4v5h5"/><path d="M12 7v5l4 2"/>',
    "Database": '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 1.7 4 3 9 3s9-1.3 9-3V5M3 12c0 1.7 4 3 9 3s9-1.3 9-3"/>',
    "Settings": '<path d="M10 2h4l.7 2.5 2 1 2.4-.8 2 3.4-1.9 1.8v2.2l1.9 1.8-2 3.4-2.4-.8-2 1L14 22h-4l-.7-2.5-2-1-2.4.8-2-3.4 1.9-1.8v-2.2L2.9 8.1l2-3.4 2.4.8 2-1z"/><circle cx="12" cy="12" r="3"/>',
}

def navigation_icon(name, filename):
    uri = asset_data(filename)
    if uri:
        return uri
    markup = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
              'fill="none" stroke="#123F84" stroke-width="1.9" stroke-linecap="round" '
              'stroke-linejoin="round">' + NAV_ICON_PATHS[name] + '</svg>')
    return 'data:image/svg+xml;base64,' + base64.b64encode(markup.encode('utf-8')).decode('ascii')

nav_styles = []
for nav_name, nav_filename in (("Dashboard", "dashboard.svg"), ("History", "history.svg"),
                               ("Database", "database.svg"), ("Settings", "settings.svg")):
    uri = navigation_icon(nav_name, nav_filename)
    nav_styles.append(
        f'[data-testid="stSidebar"] [class*="st-key-nav_{nav_name}"] button {{'
        f'background-image:url("{uri}") !important;'
        'background-repeat:no-repeat !important;'
        'background-size:26px 26px !important;'
        'background-position:18px center !important;'
        'padding-left:49px !important;'
        '}'
    )

sidebar_background = f'url("{sidebar_bg_uri}")' if sidebar_bg_uri else "none"

render_html(f"""
<style>
.insight-accent {{
    background: linear-gradient(90deg, #1666D9 0%, #4FA8FF 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    font-weight: 800;
}}

/* Base / App */
:root {{
    --primary-color: #126CE4;
    --secondary-background-color: #EFF8FF;
}}

html, body, .stApp, .stApp *,
[data-testid="stSidebar"], [data-testid="stSidebar"] *,
[data-testid="stMainBlockContainer"], [data-testid="stMainBlockContainer"] * {{
    font-family: Arial, "Segoe UI", sans-serif !important;
    letter-spacing: normal !important;
}}

#MainMenu, footer {{
    visibility: hidden;
}}

.stApp {{
    background: {background_layer} linear-gradient(
        130deg,
        #FAFDFF 0%,
        #ECF9FF 28%,
        #DDF3FF 62%,
        #FFFFFF 100%
    ) !important;
}}

[data-testid="stAppViewContainer"],
[data-testid="stHeader"] {{
    background: transparent !important;
}}

[data-testid="stMainBlockContainer"] {{
    max-width: 100% !important;
    padding-top: 12px !important;
}}

/* Force a consistent light UI across Safari/Chrome and device dark mode. */
html, body, .stApp {{
    color-scheme: light !important;
}}

[data-testid="stTextInput"] [data-baseweb="base-input"],
[data-testid="stTextInput"] [data-baseweb="input"],
[data-testid="stTextArea"] [data-baseweb="base-input"],
[data-testid="stTextArea"] [data-baseweb="textarea"] {{
    background-color: #FFFFFF !important;
    border-color: #D7E5F4 !important;
}}

[data-testid="stTextInput"] input,
[data-testid="stTextArea"] textarea {{
    background-color: #FFFFFF !important;
    color: #173B69 !important;
    -webkit-text-fill-color: #173B69 !important;
    caret-color: #126CE4 !important;
    opacity: 1 !important;
}}

[data-testid="stTextInput"] input:disabled,
[data-testid="stTextArea"] textarea:disabled {{
    background-color: #F7FAFE !important;
    color: #173B69 !important;
    -webkit-text-fill-color: #173B69 !important;
    opacity: 1 !important;
}}

[data-testid="stTextInput"] input::placeholder,
[data-testid="stTextArea"] textarea::placeholder {{
    color: #8B9DB3 !important;
    -webkit-text-fill-color: #8B9DB3 !important;
    opacity: 1 !important;
}}

[data-testid="stTextInput"] input:-webkit-autofill,
[data-testid="stTextInput"] input:-webkit-autofill:hover,
[data-testid="stTextInput"] input:-webkit-autofill:focus {{
    -webkit-box-shadow: 0 0 0 1000px #FFFFFF inset !important;
    -webkit-text-fill-color: #173B69 !important;
}}

[data-baseweb="select"] > div,
[data-testid="stSelectbox"] [data-baseweb="select"] > div {{
    background-color: #FFFFFF !important;
    color: #173B69 !important;
    border-color: #D7E5F4 !important;
}}

[data-baseweb="popover"],
[data-baseweb="menu"] {{
    color-scheme: light !important;
}}

@media (prefers-color-scheme: dark) {{
    .stApp,
    [data-testid="stAppViewContainer"] {{
        color: #173B69 !important;
    }}

    [data-testid="stTextInput"] [data-baseweb="base-input"],
    [data-testid="stTextInput"] [data-baseweb="input"] {{
        background-color: #FFFFFF !important;
    }}
}}

[data-testid="stMainBlockContainer"] p,
[data-testid="stMainBlockContainer"] label {{
    font-size: 16px !important;
    line-height: 1.5 !important;
}}

[data-testid="stMainBlockContainer"] h1 {{
    font-size: 39px !important;
    font-weight: 800 !important;
}}

[data-testid="stMainBlockContainer"] h2 {{
    font-size: 28px !important;
    font-weight: 750 !important;
}}

[data-testid="stMainBlockContainer"] h3 {{
    font-size: 22px !important;
    font-weight: 750 !important;
}}

.stDataFrame {{
    border-radius: 11px;
    overflow: hidden;
}}

/* Keep Material Symbols usable after the global font rule. */
[data-testid="stIconMaterial"],
.material-symbols-rounded,
.material-symbols-outlined {{
    font-family: "Material Symbols Rounded", "Material Symbols Outlined", "Material Icons" !important;
    font-feature-settings: "liga" !important;
    letter-spacing: normal !important;
    font-style: normal !important;
}}

/* ===== Sidebar ===== */
section[data-testid="stSidebar"] {{
    background-color: #F5FBFF !important;
    background-image:
        linear-gradient(
            180deg,
            rgba(236, 247, 255, 0.26) 0%,
            rgba(223, 240, 255, 0.25) 34%,
            rgba(209, 231, 255, 0.10) 100%
        ),
        {sidebar_background};
    background-size: 100% 100%, 100% 100% !important;
    background-position: center top, center bottom !important;
    background-repeat: no-repeat, no-repeat !important;
    min-width: 0;
}}

[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] {{
    min-height: 100vh;
    padding: 2px 16px 20px !important;
    margin-top: -24px !important;
}}

[data-testid="stSidebar"] .brand {{
    display: flex !important;
    flex-direction: column !important;
    align-items: flex-start !important;
    justify-content: flex-start !important;
    width: 100% !important;
    min-height: 165px;
    margin: -46px 0 26px 0px !important;    
    padding: 0 !important;
    gap: 2px !important;
    text-align: left !important;
}}

[data-testid="stSidebar"] .brand-logo {{
    display: block !important;
    width: 130px !important;
    min-width: 0 !important;
    max-width: 130px !important;
    height: auto !important;
    max-height: none !important;
    object-fit: contain !important;
    object-position: left top !important;
    margin: 15px 0 0 20px !important;
    transform: none !important;
    position: relative !important;
    left: -35px !important;
    
}}

[data-testid="stSidebar"] .brand-subtitle {{
    color: #6F7F92 !important;
    font-size: 14px !important;
    font-weight: 500 !important;
    line-height: 1.1 !important;
    margin-top: -12px !important;
    margin-left: 0 !important;
    text-align: left !important;
    position: relative !important;
    left: -5px !important;}}

.brand-placeholder {{
    font-size: 27px;
    font-weight: 800;
    color: #103875;
}}

[data-testid="stSidebar"] .stButton {{
    margin: 0 0 7px !important;
    transform: translateY(-30px);
}}

[data-testid="stSidebar"] .stButton button {{
    box-sizing: border-box !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
    width: 100% !important;
    height: 69px !important;
    min-height: 69px !important;
    padding: 0 12px 0 49px !important;
    font-family: "Segoe UI", Arial, sans-serif !important;
    font-size: 19px !important;
    font-weight: 700 !important;
    line-height: 1.15 !important;
    letter-spacing: 0 !important;
    text-align: center !important;
    color: #102C58 !important;
    background-color: rgba(255,255,255,.96) !important;
    border: 1.5px solid #D9E8F7 !important;
    border-radius: 16px !important;
    box-shadow: 0 3px 10px rgba(22,80,149,.05) !important;
    transition: background-color .2s ease, border-color .2s ease;
}}

[data-testid="stSidebar"] .stButton button[kind="primary"] {{
    background-color: #D1E9FF !important;
    color: #0954AE !important;
    border: 2px solid #2181EE !important;
    box-shadow: 0 4px 13px rgba(31,109,204,.13) !important;
}}

[data-testid="stSidebar"] .stButton button:hover {{
    border-color: #83BAEC !important;
}}

[data-testid="stSidebar"] .stButton button[kind="primary"]:hover {{
    background-color: #C7E4FF !important;
}}

[data-testid="stSidebar"] .stButton button::before {{
    display: none !important;
    content: none !important;
}}

[data-testid="stSidebar"] .stButton button p {{
    font: inherit !important;
    color: inherit !important;
    margin: 0 !important;
}}

{''.join(nav_styles)}

[data-testid="stMainBlockContainer"] .stButton button[kind="primary"],
[data-testid="stMainBlockContainer"] .stDownloadButton button[kind="primary"],
[data-testid="stMainBlockContainer"] [data-testid="stFileUploaderDropzone"] button {{
    background-color: #126CE4 !important;
    border-color: #126CE4 !important;
    color: #FFFFFF !important;
}}

[data-testid="stMainBlockContainer"] .stButton button[kind="primary"]:hover,
[data-testid="stMainBlockContainer"] .stDownloadButton button[kind="primary"]:hover {{
    background-color: #0754BD !important;
    border-color: #0754BD !important;
}}

[data-testid="stMainBlockContainer"] .stButton button,
[data-testid="stMainBlockContainer"] .stDownloadButton button {{
    font-size: 16px !important;
}}

[data-testid="stMainBlockContainer"] [data-testid="stToggle"] [role="switch"][aria-checked="true"],
[data-testid="stMainBlockContainer"] [data-testid="stToggle"] [data-baseweb="switch"]:has(input:checked) > div,
[data-testid="stMainBlockContainer"] [role="switch"][aria-checked="true"],
[data-testid="stMainBlockContainer"] [data-testid="stCheckbox"] input:checked + div,
[data-testid="stMainBlockContainer"] [data-testid="stCheckbox"] input:checked + span {{
    background: #126CE4 !important;
    border-color: #126CE4 !important;
}}

[data-testid="stMainBlockContainer"] input[type="checkbox"] {{
    accent-color: #126CE4 !important;
}}

{action_icons_css}

.st-key-invoice_viewer,
.st-key-extraction_panel {{
    min-height: 655px !important;
}}

.st-key-invoice_viewer,
.st-key-extraction_panel,
.st-key-validation_summary_panel,
.st-key-dashboard_actions_panel,
.st-key-validation_results_panel {{
    background: rgba(255,255,255,.98) !important;
    border: 1px solid #DFE8F5 !important;
    border-radius: 14px !important;
    box-shadow: 0 2px 8px rgba(27,83,148,.035) !important;
}}

.st-key-validation_results_panel {{
    width: 100% !important;
    margin-top: -.35rem !important;
}}

.st-key-dashboard_actions_panel .stButton button,
.st-key-dashboard_actions_panel .stDownloadButton button {{
    min-height: 48px;
    border: 1.5px solid #1269E3;
    color: #1269E3;
    font-weight: 650;
}}

.st-key-dashboard_actions_panel .stButton button[kind="primary"] {{
    color: #FFFFFF !important;
}}

.section-title {{
    display: flex;
    align-items: center;
    font-size: 20px;
    font-weight: 760;
    color: #153C73;
    margin-bottom: 12px;
}}

.section-symbol {{
    width: 24px;
    height: 24px;
    object-fit: contain;
    vertical-align: middle;
    margin-right: 8px;
}}

/* ===== Page hero ===== */
.page-hero {{
    background: linear-gradient(
        105deg,
        rgba(247,252,255,.92) 0%,
        rgba(186,224,253,.83) 58%,
        rgba(143,200,245,.70) 100%
    ) !important;
    border: 1px solid rgba(255,255,255,.6);
    border-radius: 17px;
    padding: 16px 23px 21px !important;
    margin: 0 0 15px !important;
    color: #0C246B;
}}

.page-hero h1 {{
    margin: 0;
    font-size: 39px !important;
    font-weight: 800;
    line-height: 1.18 !important;
    color: #0D256D;
}}

.page-hero p {{
    margin: 3px 0 0;
    font-size: 17px !important;
    color: #436B9E;
}}

/* ===== Dashboard KPI cards ===== */
.kpi-card {{
    position: relative;
    overflow: hidden;
    border-radius: 18px;
    padding: 18px 18px 16px;
    min-height: 170px;
    border: 1px solid rgba(255,255,255,.7);
    box-shadow: 0 4px 14px rgba(34, 82, 140, 0.06);
    align-items: center !important;
    text-align: center !important;

}}
.kpi-saved {{
    background: linear-gradient(145deg, #F2EEFF 0%, #E9E8FF 100%);
}}

.kpi-validated {{
    background: linear-gradient(145deg, #E9FFF5 0%, #D9F8EC 100%);
}}

.kpi-review {{
    background: linear-gradient(145deg, #FFF8E9 0%, #FFF0D3 100%);
}}

.kpi-modified {{
    background: linear-gradient(145deg, #EAF5FF 0%, #DCEEFF 100%);
}}

.kpi-card::after {{
    content: "";
    position: absolute;
    left: -10%;
    bottom: -48px;
    width: 120%;
    height: 85px;
    border-radius: 50% 50% 0 0;
    background: rgba(255,255,255,.28);
    transform: rotate(-3deg);
    pointer-events: none;
}}

.kpi-top {{
    display: flex;
    align-items: center;
    gap: 12px;
    position: relative;
    z-index: 2;
}}

.kpi-icon {{
    width: 50px;
    height: 50px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
}}

.kpi-icon img {{
    width: 27px;
    height: 27px;
}}

.kpi-label {{
    font-size: 18px !important;
    font-weight: 750 !important;
    color: #173B69;
}}

.kpi-arrow {{
    margin-left: auto;
    font-size: 27px;
    font-weight: 400;
    color: #4F709C;
}}

.kpi-number-row {{
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 12px;
    margin-top: 10px;
    position: relative;
    z-index: 2;
}}

.kpi-value {{
    font-size: 40px !important;
    font-weight: 770 !important;
    color: #0D2E68;
    margin: 0 !important;
    width: auto !important;
}}

.kpi-badge {{
    padding: 4px 10px;
    border-radius: 20px;
    font-size: 13px;
    font-weight: 750;
}}

.kpi-validated .kpi-badge {{
    background: #BDF4D7;
    color: #17A05B;
}}

.kpi-review .kpi-badge {{
    background: #FFE3AF;
    color: #D88612;
}}

.kpi-modified .kpi-badge {{
    background: #C9E7FF;
    color: #2889DF;
}}

.kpi-note {{
    position: relative;
    z-index: 2;
    font-size: 14px !important;
    line-height: 1.35;
    color: #6F86A3;
    margin-top: 8px;
}}
/* FIX KPI LAYOUT */
.kpi-grid {{
    display: grid !important;
    grid-template-columns: repeat(4, minmax(0, 1fr)) !important;
    gap: 14px !important;
    width: 100% !important;
    margin: 16px 0 18px !important;
    align-items: stretch !important;
}}

.kpi-grid .kpi-card {{
    width: auto !important;
    min-width: 0 !important;
    margin: 0 !important;
}}

/* ===== Upload / invoice viewer ===== */
.st-key-upload_panel {{
    background: #FFFFFF;
    border: 1px solid #E0EBF8 !important;
    border-radius: 16px !important;
    padding: 15px !important;
    box-shadow: 0 3px 12px rgba(27,83,148,.04);
}}

.st-key-upload_panel section[data-testid="stFileUploaderDropzone"] {{
    min-height: 180px !important;
    padding: 22px 16px !important;
    background: #F8FBFF !important;
    border: 2px dashed #9DC9F4 !important;
    border-radius: 14px !important;
}}

.st-key-upload_panel section[data-testid="stFileUploaderDropzone"] button {{
    min-height: 44px !important;
    white-space: nowrap !important;
    background: #126CE4 !important;
    border: 1px solid #126CE4 !important;
    border-radius: 9px !important;
    color: #FFFFFF !important;
    font-size: 16px !important;
    font-weight: 650 !important;
}}

.st-key-upload_panel section[data-testid="stFileUploaderDropzone"] button:hover {{
    background: #0754BD !important;
}}

.st-key-upload_panel [data-testid="stFileUploaderDropzoneInstructions"] {{
    color: #153C73 !important;
}}

.st-key-upload_panel [data-testid="stLinkButton"] a {{
    background: #EAF4FF !important;
    border: 1px solid #A8CEF4 !important;
    border-radius: 9px !important;
    color: #1769C5 !important;
    font-weight: 650 !important;
}}

.st-key-upload_panel [data-testid="stLinkButton"] a:hover {{
    background: #D7EAFF !important;
}}

.st-key-backend_loading_suppressed [data-testid="stSpinner"] {{
    display: none !important;
}}

.st-key-invoice_viewer div[data-testid="stImage"] {{
    display: flex;
    justify-content: center;
}}

.st-key-invoice_viewer div[data-testid="stImage"] img {{
    width: 100% !important;
    max-width: 850px !important;
    height: auto !important;
    object-fit: contain !important;
}}

/* ===== Extraction fields ===== */
.st-key-extraction_panel [data-testid="stVerticalBlock"] {{
    gap: .30rem !important;
}}

.st-key-extraction_panel [data-testid="stMarkdownContainer"] p {{
    margin: 0 !important;
}}

.st-key-extraction_panel [data-testid="stMarkdownContainer"] strong {{
    font-size: 15px !important;
}}

.st-key-extraction_panel [data-testid="stTextInput"] input {{
    min-height: 38px !important;
    height: 38px !important;
    padding-block: .35rem !important;
    font-size: 15px !important;
}}

.st-key-extraction_panel .save-feedback {{
    padding: 6px 10px;
    border-radius: 7px;
    margin: 1px 0 2px;
    font-size: 13px;
    line-height: 1.35;
    font-weight: 600;
}}

.st-key-extraction_panel .save-feedback--pending {{
    background: #FFF0F0;
    color: #B42318;
    border-left: 3px solid #D92D20;
}}

.st-key-extraction_panel .save-feedback--saved {{
    background: #E7F6EC;
    color: #167445;
    border-left: 3px solid #249C64;
}}

.st-key-extraction_panel:has(input:focus) .st-key-save_field_changes_btn button:not(:disabled) {{
    background: #176CE4 !important;
    color: #FFFFFF !important;
    border-color: #176CE4 !important;
}}

[data-testid="stMainBlockContainer"] .st-key-extraction_panel .field-status {{
    font-size: 14px !important;
}}

div[class*="st-key-invoice_field_row_"] [data-testid="stVerticalBlock"] {{
    gap: .05rem !important;
}}

div[class*="st-key-invoice_field_row_"] [data-testid="stHorizontalBlock"] {{
    align-items: flex-start !important;
    gap: .5rem !important;
}}

div[class*="st-key-invoice_field_row_"] [data-testid="stMarkdownContainer"] p {{
    margin: 0 !important;
}}

div[class*="st-key-invoice_field_row_"] [data-testid="stTextInput"] {{
    margin-bottom: 0 !important;
}}

.field-status {{
    display: flex;
    align-items: center;
    justify-content: center;
    width: 100%;
    height: 38px;
    min-height: 38px;
    padding: 0 4px;
    margin-top: 0;
    border-radius: 8px;
    text-align: center;
    font-size: 15px;
    font-weight: 650;
}}

.field-name-slot {{
    display: flex;
    align-items: center;
    height: 38px;
    min-height: 38px;
}}

.original-slot {{
    margin-top: 0 !important;
    padding: 0;
    overflow: hidden;
    white-space: nowrap;
    text-overflow: ellipsis;
    font-size: 12px;
    line-height: 15px;
    color: #7890AD;
}}

.status-valid {{
    background: #E5F8EF;
    color: #16845A;
}}

.status-review {{
    background: #FFF1DB;
    color: #BD7814;
}}

.status-modified {{
    background: #EAF2FF;
    color: #226BC2;
}}

/* ===== Validation table ===== */
.st-key-validation_results_panel .validation-table-wrap {{
    width: 100% !important;
    overflow-x: auto;
    background: #FFFFFF;
    border: 1px solid #DCE9F7;
    border-radius: 10px;
}}

.st-key-validation_results_panel .validation-table {{
    width: 100% !important;
    min-width: 950px !important;
    border-collapse: collapse;
    table-layout: fixed;
    font-family: "Segoe UI", Arial, sans-serif !important;
    color: #153461;
    font-size: 13px;
}}

.st-key-validation_results_panel .validation-table thead th {{
    height: 37px;
    padding: 9px !important;
    background: #EAF5FF !important;
    color: #103465 !important;
    border-bottom: 1px solid #D6E4F2;
    font-size: 13px !important;
    font-weight: 700 !important;
    text-align: left !important;
}}

.st-key-validation_results_panel .validation-table th:nth-child(1),
.st-key-validation_results_panel .validation-table td:nth-child(1) {{
    width: 4%;
    text-align: center !important;
}}

.st-key-validation_results_panel .validation-table th:nth-child(2),
.st-key-validation_results_panel .validation-table td:nth-child(2) {{ width: 13%; }}

.st-key-validation_results_panel .validation-table th:nth-child(3),
.st-key-validation_results_panel .validation-table td:nth-child(3) {{ width: 16%; }}

.st-key-validation_results_panel .validation-table th:nth-child(4),
.st-key-validation_results_panel .validation-table td:nth-child(4) {{ width: 16%; }}

.st-key-validation_results_panel .validation-table th:nth-child(5),
.st-key-validation_results_panel .validation-table td:nth-child(5) {{ width: 15%; }}

.st-key-validation_results_panel .validation-table th:nth-child(6),
.st-key-validation_results_panel .validation-table td:nth-child(6) {{ width: 36%; }}

.st-key-validation_results_panel .validation-table td {{
    height: 37px;
    padding: 7px 9px !important;
    vertical-align: middle;
    text-align: left;
    background: #FFFFFF;
    border-bottom: 1px solid #E5EEF8;
    overflow-wrap: break-word;
    line-height: 1.3;
}}

.st-key-validation_results_panel .validation-table tbody tr:nth-child(even) td {{
    background: #F8FBFF;
}}

.st-key-validation_results_panel .validation-table tbody tr:hover td {{
    background: #F0F7FF;
}}

.st-key-validation_results_panel .validation-table tbody tr:last-child td {{
    border-bottom: none;
}}

.st-key-validation_results_panel .validation-table .result-pill {{
    display: inline-flex !important;
    align-items: center;
    justify-content: center;
    gap: 4px;
    min-width: 100px;
    padding: 4px 8px;
    border: 1px solid transparent;
    border-radius: 999px;
    font-size: 12px !important;
    font-weight: 700 !important;
    line-height: 1.2;
    white-space: nowrap;
}}

.st-key-validation_results_panel .validation-table .status-ok {{
    background: #E0F8ED !important;
    color: #087B50 !important;
    border-color: #BBECD5;
}}

.st-key-validation_results_panel .validation-table .status-attention {{
    background: #FFF0D8 !important;
    color: #A9640A !important;
    border-color: #F7D59E;
}}

.st-key-validation_results_panel .validation-table .status-edited {{
    background: #E5F1FF !important;
    color: #175FBC !important;
    border-color: #C5DFFF;
}}

.st-key-validation_results_panel .validation-table .modified-value {{
    display: inline-block;
    padding: 3px 7px;
    background: #E6F2FF;
    color: #145FAF;
    border: 1px solid #C9E1FB;
    border-radius: 7px;
    font-weight: 650;
}}

.st-key-validation_results_panel .validation-table .empty-modification {{
    color: #9AA9BB;
}}

/* ===== Database table ===== */
.db-scroll {{
    overflow-x: auto;
    background: #FFFFFF;
    border: 1px solid #D8E6F7;
    border-radius: 12px;
}}

.db-record-table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 14px;
    color: #153969;
}}

.db-record-table th {{
    background: #ECF5FF;
    color: #112D60;
    font-weight: 750;
    text-align: left;
    white-space: nowrap;
}}

.db-record-table th,
.db-record-table td {{
    padding: 12px 11px;
    border-bottom: 1px solid #E2EAF5;
    vertical-align: middle;
}}

.db-record-table tbody tr:nth-child(even) {{ background: #F7FBFF; }}
.db-record-table tbody tr:hover {{ background: #EBF5FF; }}
.db-record-table tr:last-child td {{ border-bottom: 0; }}

.st-key-database_deletion_panel {{
    padding: 15px;
    background: #FFFFFF;
    border: 1px solid #E0EAF5;
    border-radius: 14px;
}}

/* ===== Responsive ===== */
@media (max-width: 1180px) {{
    .kpi-grid {{
        grid-template-columns: repeat(2, minmax(0, 1fr)) !important;
    }}

    /* Dashboard: invoice + extracted info on first row, summary/actions below. */
    [data-testid="stHorizontalBlock"]:has(.st-key-invoice_viewer) {{
        flex-wrap: wrap !important;
        align-items: stretch !important;
    }}

    [data-testid="stHorizontalBlock"]:has(.st-key-invoice_viewer) > [data-testid="stColumn"] {{
        flex: 1 1 calc(50% - .5rem) !important;
        width: calc(50% - .5rem) !important;
        min-width: 0 !important;
    }}

    [data-testid="stHorizontalBlock"]:has(.st-key-invoice_viewer) > [data-testid="stColumn"]:has(.st-key-validation_summary_panel) {{
        flex-basis: 100% !important;
        width: 100% !important;
    }}

    .st-key-invoice_viewer,
    .st-key-extraction_panel {{
        min-height: 0 !important;
        height: auto !important;
    }}
}}

@media (max-width: 900px) {{
    [data-testid="stSidebar"][aria-expanded="true"] {{
        width: 286px !important;
        min-width: 286px !important;
        max-width: 286px !important;
    }}

    [data-testid="stSidebar"] .brand-logo {{
        width: 118px !important;
        max-width: 118px !important;
    }}

    [data-testid="stMainBlockContainer"] {{
        padding-left: 14px !important;
        padding-right: 14px !important;
    }}

    .page-hero {{
        padding: 14px 17px 18px !important;
    }}

    .page-hero h1 {{
        font-size: 32px !important;
    }}
}}

@media (max-width: 760px) {{
    .kpi-grid {{
        grid-template-columns: 1fr !important;
        gap: 10px !important;
    }}

    .kpi-card {{
        min-height: 145px !important;
    }}

    [data-testid="stHorizontalBlock"]:has(.st-key-invoice_viewer) > [data-testid="stColumn"] {{
        flex: 1 1 100% !important;
        width: 100% !important;
    }}

    .page-hero h1 {{
        font-size: 27px !important;
    }}

    .page-hero p {{
        font-size: 15px !important;
    }}

    .section-title {{
        font-size: 18px !important;
    }}

    .st-key-upload_panel section[data-testid="stFileUploaderDropzone"] {{
        min-height: 150px !important;
        padding: 16px 12px !important;
    }}

    .st-key-validation_results_panel .validation-table-wrap,
    .db-scroll {{
        -webkit-overflow-scrolling: touch;
    }}
}}
</style>
""", unsafe_allow_html=True)
with st.sidebar:
    brand_content = f'<img class="brand-logo" src="{logo_uri}" alt="InvoSight">' if logo_uri else '<span class="brand-placeholder">InvoSight</span>'
    render_html(f'<div class="brand">{brand_content}<div class="brand-subtitle">AI Invoice Intelligence</div></div>', unsafe_allow_html=True)
    pages = {"Dashboard": "dashboard.svg", "History": "history.svg", "Database": "database.svg", "Settings": "settings.svg"}
    for name, filename in pages.items():
        if st.button(name, key=f"nav_{name}", use_container_width=True,
                     type="primary" if st.session_state.page == name else "secondary"):
            st.session_state.page = name
            st.rerun()
FIELD_LABELS = {
    "invoice_number": "Invoice Number",
    "invoice_date": "Invoice Date",
    "due_date": "Due Date",
    "vendor_name": "Vendor Name",
    "customer_name": "Customer Name",
    "subtotal": "Subtotal",
    "discount": "Discount",
    "tax": "Tax Amount",
    "total": "Total Amount",
}
SAMPLE_FIELDS = {
    key: "" for key in FIELD_LABELS
}
if "saved_fields" not in st.session_state:
    st.session_state.saved_fields = SAMPLE_FIELDS.copy()
if "original_fields" not in st.session_state:
    st.session_state.original_fields = None
if "extraction_complete" not in st.session_state:
    st.session_state.extraction_complete = False
if "ocr_lines" not in st.session_state:
    st.session_state.ocr_lines = []
if "ocr_status" not in st.session_state:
    st.session_state.ocr_status = ""
if "discount_rate" not in st.session_state:
    st.session_state.discount_rate = None
if "tax_rate" not in st.session_state:
    st.session_state.tax_rate = None
if "line_items" not in st.session_state:
    st.session_state.line_items = None
if "edit_fields" not in st.session_state:
    st.session_state.edit_fields = False
if "manual_tax_override" not in st.session_state:
    st.session_state.manual_tax_override = False
if "edit_error" not in st.session_state:
    st.session_state.edit_error = ""
if "edit_note" not in st.session_state:
    st.session_state.edit_note = ""
if "live_field_revision" not in st.session_state:
    st.session_state.live_field_revision = {key: 0 for key in FIELD_LABELS}
if st.session_state.get("stable_input_version") != 2:
    st.session_state.stable_input_version = 2
    if st.session_state.get("extraction_complete"):
        saved_nonempty = [field for field in FIELD_LABELS
                          if str(st.session_state.saved_fields.get(field, "")).strip()]
        blank_count = sum(
            not str(st.session_state.get(f"field_{field}", "")).strip()
            for field in saved_nonempty
        )
        orphaned = bool(saved_nonempty) and blank_count >= max(2, len(saved_nonempty) // 2)
        for field in FIELD_LABELS:
            widget_key = f"field_{field}"
            if widget_key not in st.session_state or orphaned:
                st.session_state[widget_key] = str(
                    st.session_state.saved_fields.get(field, "")
                )
def refresh_live_input(key: str):
    """Re-create ONLY an input changed programmatically (calculated or reset)."""
    revisions = st.session_state.setdefault(
        "live_field_revision", {field: 0 for field in FIELD_LABELS}
    )
    revisions[key] = revisions.get(key, 0) + 1
def draft_fields():
    """Live view of current widget inputs only while Edit Mode is enabled."""
    if not st.session_state.extraction_complete or not st.session_state.edit_fields:
        return deepcopy(st.session_state.saved_fields)
    return {key: st.session_state.get(f"field_{key}",
                                      st.session_state.saved_fields.get(key, ""))
            for key in FIELD_LABELS}
def on_edit_toggle():
    """Rehydrate the SAME native widgets from saved values when editing starts.
    Do not reuse removed readonly_* keys: those caused all inputs to
    appear empty and erroneously count as Modified in the previous version.
    """
    if st.session_state.edit_fields and st.session_state.extraction_complete:
        for field in FIELD_LABELS:
            st.session_state[f"field_{field}"] = str(
                st.session_state.saved_fields.get(field, "")
            )
        st.session_state.edit_error = ""
        st.session_state.edit_note = ""
def on_field_change(changed_key: str):
    """Streamlit on_change runs on Enter / when the input loses focus.
    Streamlit callbacks execute before the next script rerun. This is the safe
    point to update other widget keys (e.g. the Total Amount text input).
    """
    if not st.session_state.extraction_complete or not st.session_state.edit_fields:
        return
    st.session_state.edit_error = ""
    st.session_state.edit_note = ""
    if changed_key == "tax":
        current = draft_fields()
        st.session_state.manual_tax_override = bool(
            changed_fields(st.session_state.original_fields, current)
            & {"tax"}
        )
    if changed_key not in {"subtotal", "discount", "tax"}:
        try:
            check_editable_money(draft_fields())
        except ValueError as exc:
            st.session_state.edit_error = str(exc)
        return
    try:
        revised, note = recalculate_linked_fields(
            draft_fields(), changed_key,
            original=st.session_state.original_fields,
            tax_rate=st.session_state.tax_rate,
            manual_tax_override=st.session_state.manual_tax_override,
        )
    except ValueError as exc:
        st.session_state.edit_error = str(exc)
        return
    for key in ("tax", "total"):
        if revised[key] != st.session_state.get(f"field_{key}"):
            st.session_state[f"field_{key}"] = revised[key]
    st.session_state.edit_note = note
def reset_field_changes():
    """Reset ALL edits (including automatically recalculated amounts)."""
    original = st.session_state.get("original_fields")
    if not st.session_state.extraction_complete or original is None:
        return
    st.session_state.saved_fields = deepcopy(original)
    for key in FIELD_LABELS:
        st.session_state[f"field_{key}"] = original.get(key, "")
    st.session_state.manual_tax_override = False
    st.session_state.edit_error = ""
    st.session_state.edit_note = ""
    st.session_state.ui_flash = "Original extracted values restored."
def save_field_changes():
    """Commit changes ONLY on click; keep immutable Donut values for Reset."""
    if not st.session_state.extraction_complete or not st.session_state.edit_fields:
        return
    current = draft_fields()
    pending = changed_fields(st.session_state.saved_fields, current)
    if not pending:
        return  
    recalc_key = (
        "tax" if "tax" in pending and st.session_state.manual_tax_override
        else "discount" if "discount" in pending
        else "subtotal" if "subtotal" in pending
        else "tax" if "tax" in pending
        else None
    )
    try:
        if recalc_key is not None:
            current, note = recalculate_linked_fields(
                current, recalc_key,
                original=st.session_state.original_fields,
                tax_rate=st.session_state.tax_rate,
                manual_tax_override=st.session_state.manual_tax_override or recalc_key == "tax",
            )
            for linked_key in ("tax", "total"):
                st.session_state[f"field_{linked_key}"] = current[linked_key]
            st.session_state.edit_note = note
        check_editable_money(current)
    except ValueError as exc:
        st.session_state.edit_error = str(exc)
        return
    st.session_state.edit_error = ""
    st.session_state.saved_fields = deepcopy(current)
    st.session_state.ui_flash = "Changes saved successfully."
def current_validation(fields=None):
    """One live source for field badges, the chart and the results table."""
    if not st.session_state.extraction_complete:
        return {key: {"status": "Not Extracted", "rule": "Awaiting extraction",
                      "reason_code": "not_extracted",
                      "message": "Upload an invoice and run extraction first."}
                for key in FIELD_LABELS}
    return validate_fields(
        draft_fields() if fields is None else fields,
        items=st.session_state.line_items,
        discount_rate=st.session_state.discount_rate,
        tax_rate=st.session_state.tax_rate,
        ocr_lines=st.session_state.ocr_lines,
    )
def parse_donut_result(raw_output):
    import re
    import json
    text = str(raw_output)
    text = text.replace("<s_invoice>", "")
    text = text.replace("</s_invoice>", "")
    text = text.replace("</s>", "")
    text = text.replace("<pad>", "")
    fields = {}
    for key in FIELD_LABELS:
        pattern = rf"<s_{key}>(.*?)</s_{key}>"
        match = re.search(pattern, text, re.DOTALL)
        fields[key] = (
            match.group(1).strip()
            if match
            else ""
        )
    return fields
def invoice_note(key, detail, was_modified=False):
    if was_modified:
        return "Value updated. Verify against the source before approval."
    code = detail.get("reason_code") or ""
    rules = detail.get("rule") or ""
    status = detail.get("status") or ""
    if code == "missing_value":
        return "Missing information. Check the original invoice."
    if code == "insufficient_evidence" or status == "Not Verified":
        if key in ("vendor_name", "customer_name"):
            return "Unable to fully verify this name. Check the source invoice."
        if key in ("subtotal", "discount", "tax", "total"):
            return "The amount could not be independently confirmed. Review the source invoice."
        return "Source verification is incomplete. Review the original invoice."
    if code == "mismatch" or status == "Needs Review":
        if "Source invoice_number" in rules:
            return "Invoice number differs from the source document."
        if "Source vendor_name" in rules:
            return "Supplier name differs from the source document."
        if "Source customer_name" in rules:
            return "Customer name differs from the source document."
        if "Source " in rules and key in ("invoice_date", "due_date"):
            return "Date differs from the source document."
        if "Due Date Chronology" in rules:
            return "Due date is earlier than the invoice date."
        if "Source " in rules and key in ("subtotal", "discount", "tax", "total"):
            return "Amount differs from the source document. Please review."
        if "Equation" in rules:
            return "The calculated amount requires review."
        return "A discrepancy was detected. Check the source invoice."
    if status == "Valid":
        if key in ("subtotal", "discount", "tax", "total"):
            return "Amount verified against available source or calculation checks."
        return "Verified against the available invoice evidence."
    return "Review this field against the source invoice."


def validation_table_markup(records, show_notes=True):
    headings = ("#", "Field Name", "Extracted Value", "Modified Value", "Status", "Note")
    header = "".join(f"<th>{escape(h)}</th>" for h in headings if show_notes or h != "Note")
    body = []
    colors = {"Valid": ("✓", "status-ok"), "Needs Review": ("!", "status-attention"),
              "Modified": ("✎", "status-edited")}
    for item in records:
        symbol, css_name = colors.get(item["Status"], ("?", "status-attention"))
        modified_value = str(item.get("Modified Value", ""))
        modified_cell = (f'<span class="modified-value">{escape(modified_value)}</span>'
                         if modified_value else '<span class="empty-modification">—</span>')
        text_cells = [f'<td class="index-cell">{item["#"]}</td>',
                      f'<td class="field-cell">{escape(item["Field Name"])}</td>',
                      f'<td class="value-cell">{escape(str(item["Extracted Value"]))}</td>',
                      f'<td class="modified-cell">{modified_cell}</td>',
                      f'<td><span class="result-pill {css_name}">{symbol} {escape(item["Status"])}</span></td>']
        if show_notes:
            text_cells.append(f'<td class="note-cell">{escape(item["Note"])}</td>')
        body.append("<tr>" + "".join(text_cells) + "</tr>")
    if not body:
        body.append(f'<tr><td colspan="{len(headings) if show_notes else len(headings)-1}" class="empty-cell">No results match your search.</td></tr>')
    return '<div class="validation-table-wrap"><table class="validation-table"><thead><tr>' + header + '</tr></thead><tbody>' + "".join(body) + '</tbody></table></div>'

page = st.session_state.page

def _database_status_badge(status):
    states = {
        "Validated": ("#E2F9EE", "#127447"),
        "Needs Review": ("#FFF1DC", "#A76608"),
        "Modified": ("#E5F2FF", "#1D64BB"),
        "Unclassified": ("#EDF1F6", "#65758C"),
    }
    background, foreground = states.get(status, states["Unclassified"])
    return (f'<span style="display:inline-flex;align-items:center;justify-content:center;'
            f'border-radius:16px;padding:4px 11px;background:{background};'
            f'color:{foreground};font-weight:650;white-space:nowrap;">'
            f'{escape(status)}</span>')


def _database_table(records):
    headers = ("Invoice Number", "Vendor", "Customer", "Invoice Date", "Total", "Last Updated", "Status", "Edits")
    head = "".join(f"<th>{escape(item)}</th>" for item in headers)
    body = []
    for row in records:
        raw_modified = row.get("modified_json") or "[]"
        try:
            changed = json.loads(raw_modified)
            modified = isinstance(changed, list) and bool(changed)
        except (TypeError, ValueError):
            modified = False
        status = row.get("validation_status") or "Unclassified"
        values = [row.get("invoice_number") or "—", row.get("vendor_name") or "—",
                  row.get("customer_name") or "—", row.get("invoice_date") or "—",
                  f'{row["total"]:,.2f}' if isinstance(row.get("total"), (int,float)) else "—",
                  (row.get("updated_at") or row.get("extracted_at") or "—")[:16].replace("T", " ")]
        cells = "".join(f"<td>{escape(str(value))}</td>" for value in values)
        cells += f"<td>{_database_status_badge(status)}</td>"
        cells += f'<td>{_database_status_badge("Modified") if modified else "—"}</td>'
        body.append(f"<tr>{cells}</tr>")
    if not body:
        body.append('<tr><td colspan="8" style="text-align:center;">No matching records.</td></tr>')
    return ("<div class='db-scroll'><table class='db-record-table'><thead><tr>"+head+
            "</tr></thead><tbody>"+"".join(body)+"</tbody></table></div>")


def render_customer_database():
    if st.session_state.pop("database_deleted_notice", None):
        st.success("Invoice records deleted successfully.")
    if st.session_state.pop("database_updated_notice", None):
        st.success("Invoice changes saved. Verification is required.")
    try:
        invoices = list_invoices(limit=1000)
    except Exception:
        logger.exception("Could not load invoice records")
        st.error("Invoice records are temporarily unavailable.")
        return
    def changed_record(record):
        try:
            changes = json.loads(record.get("modified_json") or "[]")
            return isinstance(changes, list) and bool(changes)
        except (TypeError, ValueError):
            return False
    summary = (len(invoices),
               sum(row.get("validation_status") == "Validated" for row in invoices),
               sum(row.get("validation_status") == "Needs Review" for row in invoices),
               sum(changed_record(row) for row in invoices))
    for column, title, count in zip(st.columns(4),
                                    ("Saved Invoices", "Validated", "Needs Review", "Modified Invoices"), summary):
        with column:
            with st.container(border=True):
                st.metric(title,count)
    with st.container(border=True):
        search_col, filter_col = st.columns([3,1])
        with search_col:
            search_text = st.text_input("Find an invoice",placeholder="Invoice number, vendor, customer, or file name")
        with filter_col:
            status_filter = st.selectbox("Record status",["All","Validated","Needs Review","Modified","Unclassified"])
        matching = []
        for record in invoices:
            haystack = " ".join(str(record.get(field) or "") for field in
                               ("invoice_number","vendor_name","customer_name","image_file"))
            if search_text and search_text.casefold() not in haystack.casefold():
                continue
            if status_filter == "Modified" and not changed_record(record):
                continue
            if status_filter not in ("All","Modified") and (record.get("validation_status") or "Unclassified") != status_filter:
                continue
            matching.append(record)
        st.caption(f"{len(matching)} matching invoice records")
        render_html(_database_table(matching),unsafe_allow_html=True)
    if matching:
        st.markdown("#### Invoice Details")
        options = {f"{record.get('invoice_number') or record.get('image_file') or 'Invoice'} · #{record['id']}": record
                   for record in matching}
        chosen = st.selectbox("Select a saved invoice",list(options),key="database_selected_invoice")
        selected = options[chosen]
        with st.container(border=True):
            number_col, state_col = st.columns(2)
            number_col.metric("Invoice Number",selected.get("invoice_number") or "—")
            status = selected.get("validation_status") or "Unclassified"
            with state_col:
                st.markdown("**Verification Status**")
                render_html(_database_status_badge(status),unsafe_allow_html=True)
            if changed_record(selected):
                render_html(_database_status_badge("Modified"),unsafe_allow_html=True)
            st.dataframe([{"Field": label,"Value":str(selected.get(key)) if selected.get(key) is not None else "—"}
                          for key,label in FIELD_LABELS.items()],use_container_width=True,hide_index=True)
            if changed_record(selected):
                changes = json.loads(selected.get("modified_json") or "[]")
                st.info("Updated fields: " + ", ".join(FIELD_LABELS.get(field,field) for field in changes))
        with st.container(border=True):
            st.markdown("#### Edit Invoice")
            st.caption("Update saved fields. Changes are flagged for review; original extracted values are preserved.")
            with st.expander("Edit selected invoice", expanded=False):
                with st.form(key=f"database_edit_form_{selected['id']}"):
                    edit_values = {}
                    for key, label in FIELD_LABELS.items():
                        old = selected.get(key)
                        edit_values[key] = st.text_input(label, value="" if old is None else str(old),
                                                          key=f"db_edit_{selected['id']}_{key}")
                    st.info("Changes to amounts also flag related totals for review. Editing does not automatically re-verify the source invoice.")
                    save_edit = st.form_submit_button("Save Invoice Changes", type="primary", use_container_width=True)
                if save_edit:
                    try:
                        outcome = update_invoice_fields(selected["id"], edit_values)
                        if outcome["changed"]:
                            st.session_state.database_updated_notice = True
                            st.rerun()
                        else:
                            st.info("No changes were made.")
                    except ValueError as exc:
                        st.error(str(exc))
                    except Exception:
                        logger.exception("Database invoice update failed")
                        st.error("Unable to save changes. Please try again.")
        with st.container(key="database_deletion_panel"):
            st.markdown("#### Manage Records")
            st.caption("A database backup is created automatically before deleting records.")
            st.markdown(f"**Selected invoice:** {escape(str(selected.get('invoice_number') or selected.get('image_file') or selected['id']))}")
            confirm_one = st.checkbox("I confirm that I want to delete this invoice",key=f"db_confirm_{selected['id']}")
            if st.button("Delete Selected Invoice",disabled=not confirm_one,key="db_delete_selected"):
                try:
                    delete_invoice(selected["id"])
                    st.session_state.database_deleted_notice = True
                    st.rerun()
                except Exception:
                    logger.exception("Could not delete the selected invoice")
                    st.error("Unable to delete the invoice. Please try again.")
    if invoices:
        with st.expander("Delete All Invoices"):
            st.warning("This removes every stored invoice, not only the currently displayed results.")
            confirmation = st.text_input('Type DELETE ALL to confirm',key="db_delete_all_confirmation")
            if st.button("Delete All Invoices",disabled=confirmation.strip() != "DELETE ALL",key="db_delete_all"):
                try:
                    delete_all_invoices()
                    st.session_state.database_deleted_notice = True
                    st.rerun()
                except Exception:
                    logger.exception("Could not delete all invoices")
                    st.error("Unable to delete invoices. Please try again.")


if page == "Dashboard":
    hour = datetime.now(ZoneInfo("Asia/Riyadh")).hour
    if 5 <= hour < 12:
        greeting, emoji = "Good morning", "☀️"
    elif 12 <= hour < 17:
        greeting, emoji = "Good afternoon", "🌤️"
    elif 17 <= hour < 24:
        greeting, emoji = "Good evening", "🌙"
    else:
     greeting, emoji = "Good night", "🌙"
    render_html('<div class="page-hero"><div style="font-size:30px;font-weight:750;color:#173D7B;">' + greeting +
                 emoji + '<h1>From Invoices to <span class="insight-accent">Insights</span></h1><p>Clear invoice data. Reliable verification. Better financial visibility.</p></div>',
                unsafe_allow_html=True)
    try:
        stored = dashboard_stats()
        database_error = None
    except Exception as exc:
        logger.exception("Could not read the invoice database")
        stored = None
        database_error = str(exc)
    if stored is None:
        metrics = [
            ("Saved Invoices", "invoice.svg", "kpi-blue", "—", "Stored in the database and ready to access"),
            ("Validated", "check.svg", "kpi-green", "—", "Database verification successfully completed"),
            ("Needs Review", "warning.svg", "kpi-amber", "—", "Database records requiring your attention"),
            ("Modified Invoices", "edit.svg", "kpi-blue", "—", "Invoices with user-modified fields saved in the database"),
        ]
    else:
        try:
            with connection() as conn:
                modified_count = conn.execute(
                    "SELECT COUNT(*) FROM invoice_verification "
                    "WHERE modified_json IS NOT NULL "
                    "AND TRIM(modified_json) NOT IN ('', '[]', 'null')"
                ).fetchone()[0]
        except Exception:
            logger.exception("Could not calculate modified invoice count")
            modified_count = None
        metrics = [
            ("Saved Invoices", "invoice.svg", "kpi-blue", str(stored["saved"]), "Stored in the database and ready to access"),
            ("Validated", "check.svg", "kpi-green", str(stored["validated"]), "Database verification successfully completed"),
            ("Needs Review", "warning.svg", "kpi-amber", str(stored["needs_review"]), "Database records requiring your attention"),
            ("Modified Invoices", "edit.svg", "kpi-blue", str(modified_count) if modified_count is not None else "—", "Invoices with user-modified fields saved in the database"),
        ]
    cards = []
    for label, icon_file, color, value, note in metrics:
        icon_uri = asset_data(icon_file)
        card_icon_markup = f'<img src="{icon_uri}" alt="">' if icon_uri else ""
        cards.append(
            f'<div class="kpi-card {"kpi-saved" if label == "Saved Invoices" else "kpi-validated" if label == "Validated" else "kpi-review" if label == "Needs Review" else "kpi-modified"}">'
            f'<div class="kpi-top">'
            f'<div class="kpi-icon {color}">{card_icon_markup}</div>'
            f'<div class="kpi-label">{escape(label)}</div>'
            f'</div>'
            f'<div class="kpi-value">{escape(value)}</div>'
            f'<div class="kpi-note">{escape(note)}</div>'
            f'</div>'
        )
    render_html(
        '<div class="kpi-grid">' + ''.join(cards) + '</div>',
        unsafe_allow_html=True
    )
    if "upload_version" not in st.session_state:
        st.session_state.upload_version = 0
    viewer_col, info_col, side_col = st.columns(
        [1.15, 1.25, 0.85],
        gap="small",
        vertical_alignment="top"
    )
    with viewer_col:
     with st.container(border=True, key="invoice_viewer"):
        title = ("Invoice Viewer"
        if st.session_state.invoice_data is not None
        else "Upload Invoice"
        )
        render_html(
          f'<div class="section-title">{icon_html("invoice.svg")} {title}</div>',
           unsafe_allow_html=True
           )
        if st.session_state.invoice_data is None:
            st.caption("Supports PNG, JPG (Max 10 MB)")
            with st.container(key="upload_panel"):
                uploaded_invoice = st.file_uploader(
                    "Upload your invoice",
                    type=["png", "jpg", "jpeg"],
                    key=f"invoice_{st.session_state.upload_version}",
                    max_upload_size=10,
                    label_visibility="collapsed",
                    on_change=on_invoice_upload
                )
            if uploaded_invoice is not None:
                st.session_state.invoice_data = (
                    uploaded_invoice.getvalue()
                )
                st.session_state.invoice_name = (
                    uploaded_invoice.name
                )
                st.rerun()
            render_html(
                "<p style='text-align:center;"
                "color:#7890AD;margin:8px 0'></p>",
                unsafe_allow_html=True
            )
            
            
        else:
              with st.container(border=False):
                 import base64
                 image_data = base64.b64encode(
                    st.session_state.invoice_data).decode("utf-8")
                 st.image(
                 st.session_state.invoice_data,use_container_width=True)
                 st.caption(
                st.session_state.invoice_name
            )
              st.button(
                "Replace Invoice",
                icon=":material/autorenew:",
                key="replace_invoice",
                use_container_width=True,
                on_click=reset_invoice
            )
        if st.session_state.invoice_data is not None:
            if st.button(
                "Extract Information",
                key="extract_invoice_btn",
                type="primary",
                use_container_width=True
            ):
                st.session_state.extraction_complete = False
                st.session_state.original_fields = None
                st.session_state.saved_fields = SAMPLE_FIELDS.copy()
                st.session_state.ocr_lines = []
                st.session_state.ocr_status = ""
                st.session_state.discount_rate = None
                st.session_state.tax_rate = None
                st.session_state.line_items = None
                st.session_state.manual_tax_override = False
                st.session_state.edit_error = ""
                st.session_state.edit_note = ""
                st.session_state.pop("ui_flash", None)
                for key in FIELD_LABELS:
                    st.session_state.pop(f"field_{key}", None)
                try:
                    with st.spinner("Analyzing your invoice..."):
                        with st.container(key="backend_loading_suppressed"):
                            raw_output = extract_invoice(
                                st.session_state.invoice_data, unload_after=True
                            )
                        extracted = parse_donut_result(raw_output)
                    st.session_state.donut_raw_output = raw_output
                    if not any(str(v).strip() for v in extracted.values()):
                        st.error("We couldn't read this invoice. Please try a clearer image.")
                    else:
                        with st.spinner("Verifying invoice information..."):
                            ocr = run_ocr(st.session_state.invoice_data)
                        rates = extract_ocr_rates(ocr["lines"])
                        st.session_state.ocr_lines = ocr["lines"]
                        st.session_state.ocr_status = ocr["message"]
                        st.session_state.discount_rate = rates["discount_rate"]
                        st.session_state.tax_rate = rates["tax_rate"]
                        st.session_state.original_fields = deepcopy(extracted)
                        st.session_state.saved_fields = deepcopy(extracted)
                        for key, value in extracted.items():
                            st.session_state[f"field_{key}"] = value
                            refresh_live_input(key)
                        st.session_state.extraction_complete = True
                        st.session_state.edit_fields = False
                        st.session_state.manual_tax_override = False
                        st.session_state.edit_error = ""
                        st.session_state.edit_note = ""
                        st.rerun()
                except Exception as exc:
                    st.session_state.extraction_complete = False
                    logger.exception("Invoice extraction failed")
                    st.error("Invoice extraction could not be completed. Please try again.")
                    if "1455" in str(exc) or "paging file" in str(exc).lower():
                        st.info("Windows memory limit reached. Close heavy apps "
                                "and try again.")
    with info_col:
        with st.container(border=True, key="extraction_panel"):
            header_col, toggle_col = st.columns([2, 1])
            with header_col:
                render_html(
                    f"""
                    <div style="
                        font-size:19px;
                        font-weight:750;
                        color:#153C73;
                        padding-top:7px;
                    ">
                        {icon_html("invoice.svg")} Extracted Information
                    </div>
                    """,
                    unsafe_allow_html=True
                )
            with toggle_col:
                st.toggle(
                    "Edit Mode",
                    key="edit_fields",
                    on_change=on_edit_toggle,
                )
            render_html('<hr style="margin:4px 0 7px;border-color:#DBE6F5">', unsafe_allow_html=True)
            fields = draft_fields()
            field_results = current_validation(fields)
            original_results = (current_validation(st.session_state.original_fields)
                                if st.session_state.original_fields else {})
            modified = changed_fields(st.session_state.original_fields, fields)
            pending = changed_fields(st.session_state.saved_fields, fields)
            if st.session_state.get("ui_flash"):
                feedback = escape(st.session_state.pop("ui_flash"))
                render_html(
                    f'<div class="save-feedback save-feedback--saved">✓ {feedback}</div>',
                    unsafe_allow_html=True,
                )
            elif st.session_state.extraction_complete and pending:
                render_html(
                    '<div class="save-feedback save-feedback--pending">'
                    f'Unsaved Changes: {len(pending)} field(s). Save Changes to confirm.'
                    '</div>',
                    unsafe_allow_html=True,
                )
            if st.session_state.edit_error:
                st.warning(st.session_state.edit_error)
            elif st.session_state.edit_note and pending:
                st.caption(st.session_state.edit_note)
            for key in FIELD_LABELS:
                widget_key = f"field_{key}"
                if widget_key not in st.session_state:
                    st.session_state[widget_key] = str(
                        st.session_state.saved_fields.get(key, "")
                    )
            h1, h2, h3 = st.columns([1.3, 1.7, 1])
            h1.markdown("**Field Name**")
            h2.markdown("**Extracted Value**")
            h3.markdown("**Status**")
            for key, label in FIELD_LABELS.items():
                with st.container(key=f"invoice_field_row_{key}"):
                    c1, c2, c3 = st.columns(
                        [1.3, 1.7, 1],
                        gap="small", vertical_alignment="top"
                    )
                    with c1:
                        render_html(
                            f"""
                            <div class="field-name-slot" style="
                                color:#173B69;
                                font-size:14px;
                                font-weight:600;
                            ">
                                {label}
                            </div>
                            """,
                            unsafe_allow_html=True
                        )
                    with c2:
                        st.text_input(
                            label,
                            key=f"field_{key}",
                            disabled=not (st.session_state.edit_fields and
                                          st.session_state.extraction_complete),
                            label_visibility="collapsed",
                            on_change=on_field_change,
                            args=(key,),
                        )
                        if st.session_state.edit_fields and st.session_state.extraction_complete:
                            original_note = (
                                "Original: " + escape(str(st.session_state.original_fields.get(key, "")))
                                if (key in modified and original_results
                                    and public_status(original_results[key]) == "Valid") else ""
                            )
                            if original_note:
                                render_html(f'<div class="original-slot">{original_note}</div>',
                                            unsafe_allow_html=True)
                    with c3:
                        if st.session_state.extraction_complete:
                            if key in modified:
                                render_html('<div class="field-status status-modified">✎ Modified</div>',
                                            unsafe_allow_html=True)
                            else:
                                display_status = public_status(field_results[key])
                                css_class, badge_label = (
                                    ("status-valid", "✓ Valid") if display_status == "Valid"
                                    else ("status-review", "⚠ Needs Review")
                                )
                                render_html(
                                    f'<div class="field-status {css_class}">{badge_label}</div>',
                                    unsafe_allow_html=True,
                                )
                        else:
                            st.markdown("—")
            render_html('<hr style="margin:6px 0;border-color:#DBE6F5">', unsafe_allow_html=True)
            reset_col, save_col = st.columns(2)
            with reset_col:
                st.button(
                    "Reset Changes",
                    key="reset_field_changes_btn",
                    use_container_width=True,
                    disabled=not (st.session_state.edit_fields and st.session_state.extraction_complete
                                  and bool(modified)),
                    on_click=reset_field_changes,
                )
            with save_col:
              st.button(
               "Save Changes",
               type="primary" if pending else "secondary",
               use_container_width=True,
               disabled=not (st.session_state.edit_fields and st.session_state.extraction_complete),
               key="save_field_changes_btn",
               on_click=save_field_changes
    )
    with side_col:
        with st.container(border=True, key="validation_summary_panel"):
            render_html(f'<div class="section-title">{icon_html("validation-summary.svg")} Validation Summary</div>', unsafe_allow_html=True)
            current_results = current_validation()
            completed = st.session_state.extraction_complete
            visible_fields = draft_fields()
            modified_now = changed_fields(st.session_state.original_fields, visible_fields)
            counts = {"Valid": 0, "Needs Review": 0, "Modified": 0}
            if completed:
                for key, result in current_results.items():
                    if key in modified_now:
                        counts["Modified"] += 1
                    else:
                        counts[public_status(result)] += 1
            total = sum(counts.values())
            if total:
                green = 100 * counts["Valid"] / total
                orange = green + 100 * counts["Needs Review"] / total
                gradient = (f"conic-gradient(#20A36B 0% {green}%, "
                            f"#E8A43B {green}% {orange}%, "
                            f"#458ACB {orange}% 100%)")
                center = f"{counts['Valid']}/{total}"
            else:
                gradient, center = "#E8F1FC", "—"
            st.caption(("Current verification status" if st.session_state.edit_fields
                        else "Current saved values") if completed else "Awaiting extraction")
            render_html(
                f"""
                <div style="display:flex;justify-content:center;padding:15px 0;">
                  <div style="width:150px;height:150px;border-radius:50%;
                              background:{gradient};display:grid;place-items:center;">
                    <div style="width:117px;height:117px;border-radius:50%;
                                background:white;display:grid;place-items:center;
                                font-size:29px;font-weight:800;color:#1769D2;">{center}</div>
                  </div>
                </div>
                """, unsafe_allow_html=True
            )
            st.caption("Changes are shown separately until the record is verified.")
            st.divider()
            st.write(f"🟢 Valid Fields: {counts['Valid']}")
            st.write(f"🟠 Needs Review: {counts['Needs Review']}")
            st.write(f"🔵 Modified Fields: {counts['Modified']}")
        with st.container(border=True, key="dashboard_actions_panel"):
            render_html(f'<div class="section-title">{icon_html("actions.svg")} Actions</div>', unsafe_allow_html=True)
            cannot_save = (
                not st.session_state.extraction_complete
                or database_error is not None
                or bool(pending)
                or bool(st.session_state.edit_error)
            )
            if st.button(
                "Save to Database",
                key="save_database_btn",
                type="primary",
                use_container_width=True,
                disabled=cannot_save,
            ):
                try:
                    if changed_fields(st.session_state.saved_fields, draft_fields()):
                        st.warning("Save Changes first, then save the invoice to the database.")
                    else:
                        saved_validation = current_validation(st.session_state.saved_fields)
                        modified_for_database = changed_fields(
                            st.session_state.original_fields, st.session_state.saved_fields
                        )
                        result = save_invoice(
                            final_fields=st.session_state.saved_fields,
                            original_fields=st.session_state.original_fields,
                            validation=saved_validation,
                            modified=modified_for_database,
                            image_file=st.session_state.invoice_name,
                            image_bytes=st.session_state.invoice_data,
                        )
                        st.session_state.db_flash = (
                            f"Invoice {result['operation']} in database (ID: {result['id']}). "
                            f"Status: {result['status']}."
                        )
                        st.rerun()  
                except (ValueError, InvoiceFilenameConflict):
                    st.error("Please check the invoice details or file name and try again.")
                except Exception:
                    logger.exception("Failed to save invoice")
                    st.error("This invoice could not be saved. Please try again.")
            if database_error is not None:
                st.caption("Your saved invoice records are temporarily unavailable.")
            if st.session_state.get("db_flash"):
                st.success(st.session_state.pop("db_flash"))
            st.download_button(
                "Download JSON",
                data=json.dumps(
                    st.session_state.saved_fields,
                    indent=4,
                    ensure_ascii=False
                ),
                file_name="invoice.json",
                mime="application/json",
                disabled=not st.session_state.extraction_complete,
                key="download_json_btn",
                use_container_width=True
            )
            pdf_ready = (
                st.session_state.extraction_complete
                and not pending
                and not st.session_state.edit_error
            )
            if pdf_ready:
                pdf_fields = deepcopy(st.session_state.saved_fields)
                pdf_results = current_validation(pdf_fields)
                pdf_modified = changed_fields(
                    st.session_state.original_fields, pdf_fields
                )
                try:
                    report_bytes = build_verification_report(
                        current_fields=pdf_fields,
                        original_fields=st.session_state.original_fields,
                        validation_details=pdf_results,
                        changed_fields=pdf_modified,
                        invoice_file_name=st.session_state.invoice_name or "invoice",
                        public_reasons={
                            key: public_reason(result)
                            for key, result in pdf_results.items()
                        },
                    )
                except Exception:
                    logger.exception("Could not generate the verification PDF")
                    st.error("The report could not be generated. Please try again.")
                    st.button(
                        "Download Verification Report",
                        key="download_report_btn",
                        use_container_width=True,
                        disabled=True,
                    )
                else:
                    safe_invoice_name = Path(
                        st.session_state.invoice_name or "invoice"
                    ).stem
                    st.download_button(
                        "Download Verification Report",
                        key="download_report_btn",
                        data=report_bytes,
                        file_name=f"{safe_invoice_name}_verification_report.pdf",
                        mime="application/pdf",
                        icon=":material/picture_as_pdf:",
                        use_container_width=True,
                    )
            else:
                st.button(
                    "Download Verification Report",
                    key="download_report_btn",
                    use_container_width=True,
                    disabled=True,
                )
                if st.session_state.extraction_complete and pending:
                    st.caption("Save your changes before generating the PDF.")
    with st.container(border=True, key="validation_results_panel"):
        render_html(
            f'<div class="section-title">{icon_html("chart.svg")} Validation Results</div>',
            unsafe_allow_html=True,
        )
        if not st.session_state.extraction_complete:
            st.info(
                "Upload an invoice and select Extract Information "
                "to display validation results."
            )
        else:
            validation_details = current_validation()
            st.caption(
                "Live values when editing; the original invoice remains unchanged."
            )
            visible_fields = draft_fields()
            changed = changed_fields(st.session_state.original_fields, visible_fields)
            search_col, status_col = st.columns([3, 1])
            with search_col:
                validation_search = st.text_input("Search validation results", placeholder="Search by field or value", label_visibility="collapsed")
            with status_col:
                validation_status_filter = st.selectbox("Validation status", ["All Statuses", "Valid", "Needs Review", "Modified"], label_visibility="collapsed")
            validation_rows = []
            for index, (key, label) in enumerate(FIELD_LABELS.items(), start=1):
                status = "Modified" if key in changed else public_status(validation_details[key])
                note = invoice_note(key, validation_details[key], key in changed)
                row = {
                    "#": index,
                    "Field Name": label,
                    "Extracted Value": str((st.session_state.original_fields or {}).get(key, visible_fields.get(key, ""))),
                    "Modified Value": str(visible_fields.get(key, "")) if key in changed else "",
                    "Status": status,
                    "Note": note if st.session_state.ui_preferences.get("show_validation_notes", True) else "",
                }
                if validation_status_filter != "All Statuses" and row["Status"] != validation_status_filter:
                    continue
                if validation_search and validation_search.casefold() not in (label + " " + row["Extracted Value"] + " " + row["Modified Value"] + " " + note).casefold():
                    continue
                validation_rows.append(row)
            render_html(
                validation_table_markup(
                    validation_rows,
                    show_notes=st.session_state.ui_preferences.get("show_validation_notes", True)
                ),
                unsafe_allow_html=True,
            )

elif page == "History":
    render_html('<div class="page-hero"><h1>History</h1><p>Review previously saved invoices and recent updates.</p></div>', unsafe_allow_html=True)
    try:
        history = list_invoices(limit=1000)
        search_col, filter_col = st.columns([3, 1])
        with search_col:
            history_search = st.text_input("Search history", placeholder="Search invoice number, supplier, or customer")
        with filter_col:
            history_status = st.selectbox("Filter by status", ["All", "Validated", "Needs Review", "Unclassified"])
        if history_search:
            term = history_search.casefold()
            history = [record for record in history if term in " ".join(str(record.get(k) or "") for k in
                       ("invoice_number", "vendor_name", "customer_name", "image_file")).casefold()]
        if history_status != "All":
            history = [record for record in history if (record.get("validation_status") or "Unclassified") == history_status]
        st.caption(f"{len(history)} matching records")
        limit = int(st.session_state.ui_preferences.get("history_limit", 25))
        st.dataframe([
            {"Invoice Number": record.get("invoice_number") or "—",
             "Saved At": record.get("extracted_at") or "—",
             "Vendor": record.get("vendor_name") or "—",
             "Customer": record.get("customer_name") or "—",
             "Status": record.get("validation_status") or "Unclassified",
             "Last Updated": record.get("updated_at") or "—"}
            for record in history[:limit]
        ], use_container_width=True, hide_index=True)
        if history:
            options = {f"{record.get('invoice_number') or record['image_file']} · #{record['id']}": record
                       for record in history}
            choice = st.selectbox("Invoice details", list(options))
            selected = options[choice]
            with st.container(border=True):
                a, b = st.columns(2)
                a.metric("Invoice Number", selected.get("invoice_number") or "—")
                b.metric("Status", selected.get("validation_status") or "Unclassified")
                st.markdown("**Saved invoice details**")
                if selected.get("final_json"):
                    values = json.loads(selected["final_json"])
                    st.dataframe([{"Field": FIELD_LABELS.get(k, k.replace("_", " ").title()), "Value": str(v)}
                                  for k, v in values.items()], hide_index=True, use_container_width=True)
                else:
                    st.dataframe([{"Field": label, "Value": str(selected.get(key) or "—")}
                                  for key, label in FIELD_LABELS.items()], hide_index=True, use_container_width=True)
        else:
            st.info("No matching records were found.")
    except Exception:
        logger.exception("History page could not load")
        st.error("Invoice history is temporarily unavailable. Please try again.")
elif page == "Database":
    render_database_page()
    
elif page == "Settings":
    render_html('<div class="page-hero"><h1>Settings</h1><p>Manage basic workspace and display preferences.</p></div>', unsafe_allow_html=True)
    settings = dict(st.session_state.ui_preferences)
    with st.container(border=True):
        st.subheader("General Preferences")
        workspace_name = st.text_input("Workspace Name", value=str(settings.get("workspace_name", "My Workspace")),
                                       max_chars=60)
        landing_page = st.selectbox("Default Start Page", ["Dashboard", "History", "Database"],
                                    index=["Dashboard", "History", "Database"].index(
                                        settings.get("landing_page", "Dashboard")
                                        if settings.get("landing_page") in ("Dashboard", "History", "Database") else "Dashboard"))
        st.divider()
        st.subheader("Display Preferences")
        show_notes = st.toggle("Show validation notes in results", value=bool(settings.get("show_validation_notes", True)))
        history_options = [10, 25, 50, 100]
        history_limit = st.selectbox("Records displayed in History", history_options,
                                     index=history_options.index(settings.get("history_limit", 25))
                                     if settings.get("history_limit", 25) in history_options else 1)
        left, right = st.columns([1, 1])
        with left:
            if st.button("Save Settings", type="primary", use_container_width=True):
                updated = {"workspace_name": workspace_name.strip() or "My Workspace",
                           "landing_page": landing_page, "show_validation_notes": show_notes,
                           "history_limit": history_limit}
                try:
                    SETTINGS_PATH.write_text(json.dumps(updated, indent=2), encoding="utf-8")
                    st.session_state.ui_preferences = updated
                    st.success("Settings saved successfully.")
                except OSError:
                    st.error("Settings could not be saved. Check folder access.")
        with right:
            if st.button("Reset to Defaults", use_container_width=True):
                st.session_state.ui_preferences = DEFAULT_PREFERENCES.copy()
                try:
                    SETTINGS_PATH.write_text(json.dumps(DEFAULT_PREFERENCES, indent=2), encoding="utf-8")
                    st.rerun()
                except OSError:
                    st.error("Default settings could not be saved.")