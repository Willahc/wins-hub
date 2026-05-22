"""
PDF Match v2 — WiNS Hub Comercial.

Branding: preto/dourado, capa profissional, DejaVuSans (UTF-8 PT-BR), A4.
Endpoints:
  - GET /api/admin/obras/{id}/match-pdf
  - GET /api/admin/fornecedores/{cnpj}/match-pdf

Módulo standalone — não depende de PDF v0.7 legado (/api/vendas/gerar-pdf-match).
"""
from __future__ import annotations

import io
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm, mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    Image,
    KeepTogether,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

log = logging.getLogger(__name__)


# ─── Fontes UTF-8 ──────────────────────────────────────────────────────────
_FONTS_DIR = Path(__file__).parent / "fonts"
_FONT_BODY = "Helvetica"
_FONT_BOLD = "Helvetica-Bold"
_FONTS_REGISTERED = False


def _register_fonts() -> tuple[str, str]:
    """Registra DejaVuSans (UTF-8 PT-BR). Fallback Helvetica."""
    global _FONT_BODY, _FONT_BOLD, _FONTS_REGISTERED
    if _FONTS_REGISTERED:
        return _FONT_BODY, _FONT_BOLD
    regular = _FONTS_DIR / "DejaVuSans.ttf"
    bold = _FONTS_DIR / "DejaVuSans-Bold.ttf"
    if regular.exists() and bold.exists():
        try:
            pdfmetrics.registerFont(TTFont("DejaVuSans", str(regular)))
            pdfmetrics.registerFont(TTFont("DejaVuSans-Bold", str(bold)))
            _FONT_BODY, _FONT_BOLD = "DejaVuSans", "DejaVuSans-Bold"
        except Exception as e:
            log.warning("DejaVuSans register falhou: %s — fallback Helvetica", e)
    else:
        log.warning("DejaVuSans não encontrado em %s — fallback Helvetica (sem PT-BR acentos)", _FONTS_DIR)
    _FONTS_REGISTERED = True
    return _FONT_BODY, _FONT_BOLD


# ─── Paleta ────────────────────────────────────────────────────────────────
PRETO = HexColor("#0a0a0a")
DOURADO = HexColor("#fbbf24")
DOURADO_DIM = HexColor("#92400e")
CINZA_BG = HexColor("#111827")
CINZA_LT = HexColor("#374151")
CINZA_DIM = HexColor("#6b7280")
BRANCO = HexColor("#ffffff")
VERDE = HexColor("#22c55e")
VERMELHO = HexColor("#ef4444")
LARANJA = HexColor("#f97316")

TIER_COLORS = {
    "OURO": DOURADO,
    "PRATA": HexColor("#cbd5e1"),
    "BRONZE": HexColor("#b45309"),
    "PIPELINE": HexColor("#64748b"),
}


# ─── Helpers texto ─────────────────────────────────────────────────────────
_STOPWORDS = {"de", "da", "do", "das", "dos", "e", "em", "para", "com", "a", "o"}
_SIGLAS = {"SA", "S/A", "LTDA", "EIRELI", "ME", "EPP", "MEI", "EPC", "CIA", "SLU", "S.A.", "S/S"}


def titulo_empresa(s: Optional[str]) -> str:
    """Espelha window.v2helpers.tituloEmpresa (frontend v2): Title Case com siglas em CAIXA."""
    if not s:
        return "—"
    words = s.strip().split()
    out: list[str] = []
    for i, w in enumerate(words):
        clean = w.replace(".", "").replace("/", "").upper()
        if clean in _SIGLAS:
            out.append(clean)
        elif i > 0 and w.lower() in _STOPWORDS:
            out.append(w.lower())
        else:
            out.append(w.capitalize())
    return " ".join(out)


_FASE_LABELS = {
    "PLANEJAMENTO": "Planejamento",
    "LICITACAO_ABER": "Licitação Aberta",
    "LICITACAO_ABERTA": "Licitação Aberta",
    "EM_EXECUCAO": "Em Execução",
    "OPERACAO": "Em Operação",
    "CONCLUIDA": "Concluída",
    "PARALISADA": "Paralisada",
}


def humanizar_fase(fase: Optional[str]) -> str:
    if not fase:
        return "—"
    return _FASE_LABELS.get(fase, fase.replace("_", " ").title())


def format_capex(valor: Optional[float]) -> str:
    """R$ 384,0 bi · R$ 12,5 Mi · R$ 850 mil · A definir (italic dim caller-side)."""
    if valor is None:
        return "A definir"
    try:
        n = float(valor)
    except (TypeError, ValueError):
        return "A definir"
    if n >= 1e9:
        return f"R$ {n/1e9:.1f} bi".replace(".", ",")
    if n >= 1e6:
        return f"R$ {n/1e6:.1f} Mi".replace(".", ",")
    if n >= 1e3:
        return f"R$ {n/1e3:.0f} mil"
    return f"R$ {n:.0f}"


def format_data(dt) -> str:
    if not dt:
        return "—"
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except ValueError:
            return dt
    return dt.strftime("%d/%m/%Y")


# ─── Logo path (frontend v2 bind-mount) ────────────────────────────────────
def _logo_path() -> Optional[Path]:
    for candidate in (
        Path("/app/frontend/static/img/logo.png"),
        Path("/root/wins_hub_v2/app/frontend/static/img/logo.png"),
    ):
        if candidate.exists():
            return candidate
    return None


# ─── Estilos Paragraph ─────────────────────────────────────────────────────
def _build_styles() -> dict:
    body, bold = _register_fonts()
    return {
        "h1_capa": ParagraphStyle(
            "h1_capa", fontName=bold, fontSize=24, leading=28, textColor=DOURADO,
            alignment=TA_LEFT, spaceAfter=4,
        ),
        "h2_capa": ParagraphStyle(
            "h2_capa", fontName=body, fontSize=14, leading=18, textColor=BRANCO,
            alignment=TA_LEFT, spaceAfter=4,
        ),
        "obra_nome_capa": ParagraphStyle(
            "obra_nome_capa", fontName=bold, fontSize=18, leading=22, textColor=DOURADO,
            alignment=TA_LEFT, spaceBefore=20, spaceAfter=4,
        ),
        "obra_empresa_capa": ParagraphStyle(
            "obra_empresa_capa", fontName=body, fontSize=12, leading=15, textColor=BRANCO,
            alignment=TA_LEFT, spaceAfter=10,
        ),
        "secao": ParagraphStyle(
            "secao", fontName=bold, fontSize=11, leading=13, textColor=PRETO,
            alignment=TA_LEFT, spaceAfter=0,
        ),
        "h3": ParagraphStyle(
            "h3", fontName=bold, fontSize=12, leading=14, textColor=PRETO,
            alignment=TA_LEFT, spaceAfter=6, spaceBefore=10,
        ),
        "body": ParagraphStyle(
            "body", fontName=body, fontSize=10, leading=13, textColor=PRETO,
            alignment=TA_LEFT, spaceAfter=2,
        ),
        "body_dim": ParagraphStyle(
            "body_dim", fontName=body, fontSize=9, leading=12, textColor=CINZA_DIM,
            alignment=TA_LEFT,
        ),
        "body_italic_dim": ParagraphStyle(
            "body_italic_dim", fontName=body, fontSize=9, leading=12, textColor=CINZA_DIM,
            alignment=TA_LEFT, fontStyle="italic",
        ),
        "kpi_label": ParagraphStyle(
            "kpi_label", fontName=body, fontSize=8, leading=10, textColor=CINZA_DIM,
            alignment=TA_LEFT,
        ),
        "kpi_value": ParagraphStyle(
            "kpi_value", fontName=bold, fontSize=14, leading=16, textColor=DOURADO,
            alignment=TA_LEFT,
        ),
        "kpi_value_neutral": ParagraphStyle(
            "kpi_value_neutral", fontName=bold, fontSize=14, leading=16, textColor=PRETO,
            alignment=TA_LEFT,
        ),
        "kpi_value_dim": ParagraphStyle(
            "kpi_value_dim", fontName=body, fontSize=12, leading=14, textColor=CINZA_DIM,
            alignment=TA_LEFT,
        ),
        "score_grande": ParagraphStyle(
            "score_grande", fontName=bold, fontSize=20, leading=22, textColor=DOURADO,
            alignment=TA_RIGHT,
        ),
        "footer_capa": ParagraphStyle(
            "footer_capa", fontName=body, fontSize=8, leading=10, textColor=CINZA_DIM,
            alignment=TA_CENTER,
        ),
        "fornec_nome": ParagraphStyle(
            "fornec_nome", fontName=bold, fontSize=11, leading=13, textColor=PRETO,
            alignment=TA_LEFT,
        ),
        "fornec_meta": ParagraphStyle(
            "fornec_meta", fontName=body, fontSize=8, leading=10, textColor=CINZA_DIM,
            alignment=TA_LEFT,
        ),
    }


# ─── Page decoration (borda dourada na capa) ───────────────────────────────
def _draw_cover_border(canvas, doc):
    canvas.saveState()
    canvas.setFillColor(PRETO)
    canvas.rect(0, 0, A4[0], A4[1], fill=1, stroke=0)
    # Borda dourada 4px de margem
    canvas.setStrokeColor(DOURADO)
    canvas.setLineWidth(1.5)
    canvas.rect(10 * mm, 10 * mm, A4[0] - 20 * mm, A4[1] - 20 * mm, fill=0, stroke=1)
    canvas.restoreState()


def _draw_content_footer(canvas, doc):
    """Footer minimalista no rodapé das páginas internas."""
    canvas.saveState()
    body, _ = _register_fonts()
    canvas.setFillColor(CINZA_DIM)
    canvas.setFont(body, 7)
    canvas.drawCentredString(
        A4[0] / 2,
        10 * mm,
        f"WiNS Hub Comercial — Página {doc.page} — Gerado em {datetime.now().strftime('%d/%m/%Y %H:%M')}",
    )
    canvas.restoreState()


# ─── PDF Builder helpers ───────────────────────────────────────────────────
def _new_doc(buffer: io.BytesIO, title: str) -> BaseDocTemplate:
    doc = BaseDocTemplate(
        buffer,
        pagesize=A4,
        title=title,
        author="WiNS Hub Comercial",
        topMargin=1.5 * cm,
        bottomMargin=1.5 * cm,
        leftMargin=1.5 * cm,
        rightMargin=1.5 * cm,
    )
    frame_cover = Frame(
        1.5 * cm, 1.5 * cm, A4[0] - 3 * cm, A4[1] - 3 * cm,
        leftPadding=12, rightPadding=12, topPadding=12, bottomPadding=12, id="cover",
    )
    frame_content = Frame(
        1.5 * cm, 1.5 * cm, A4[0] - 3 * cm, A4[1] - 3 * cm,
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0, id="content",
    )
    doc.addPageTemplates([
        PageTemplate(id="cover", frames=[frame_cover], onPage=_draw_cover_border),
        PageTemplate(id="content", frames=[frame_content], onPage=_draw_content_footer),
    ])
    return doc


def _tier_badge_paragraph(tier: Optional[str], styles: dict) -> Paragraph:
    if not tier or tier not in TIER_COLORS:
        return Paragraph(
            f'<font color="#cbd5e1">{tier or "—"}</font>',
            styles["body"],
        )
    color = TIER_COLORS[tier]
    return Paragraph(
        f'<font color="{color.hexval()}"><b>{tier}</b></font>',
        styles["body"],
    )


def _section_header(text: str, styles: dict) -> Table:
    """Header dourado fundo + texto preto."""
    tbl = Table([[Paragraph(text.upper(), styles["secao"])]], colWidths=[A4[0] - 3 * cm])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), DOURADO),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return tbl


def _kv_row(label: str, value: str, styles: dict, value_dim: bool = False) -> list:
    """Linha [label dim] [value]."""
    return [
        Paragraph(label, styles["kpi_label"]),
        Paragraph(value, styles["kpi_value_dim"] if value_dim else styles["body"]),
    ]


def _escape(s) -> str:
    """Escape pra Paragraph: & < >. Preserva &nbsp; como Unicode NBSP
    pra que strings com &nbsp; nao sejam duplo-escapadas a &amp;nbsp;
    (renderizado literal pelo ReportLab)."""
    if s is None:
        return ""
    return (str(s)
            .replace("&nbsp;", " ")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


# Pesos da engine v2 (calcular_score_match_v2 no Postgres):
# score = (0.40*peso_cnae + 0.25*peso_uf + 0.20*peso_capex + 0.15*peso_tier) * 100
# Cada componente do score_breakdown é 0-1 (fração). Multiplicar pelo peso×100
# pra mostrar em pts somaveis ao total. Ex: peso_cnae=1.00 → 40 pts.
_BREAKDOWN_WEIGHTS_PTS = {
    "cnae": 40,
    "uf": 25,
    "capex": 20,
    "tier": 15,
}
_BREAKDOWN_LABELS = {
    "cnae": "CNAE",
    "uf": "UF",
    "capex": "Capex",
    "tier": "Tier",
}


def _format_breakdown(bd: Optional[dict]) -> str:
    """Render '<b>CNAE:</b> 40pts &nbsp; <b>UF:</b> 25pts ...' a partir do
    score_breakdown JSON (valores 0-1) ponderado pelos pesos da engine."""
    if not bd:
        return ""
    bd = bd if isinstance(bd, dict) else {}
    parts = []
    for key, weight in _BREAKDOWN_WEIGHTS_PTS.items():
        raw = bd.get(key)
        if raw is None:
            raw = bd.get(f"score_{key}")
        try:
            frac = float(raw or 0)
        except (TypeError, ValueError):
            frac = 0.0
        pts = round(frac * weight)
        parts.append(
            f'<font color="{DOURADO_DIM.hexval()}"><b>{_BREAKDOWN_LABELS[key]}:</b></font> '
            f'{pts}<font color="{CINZA_DIM.hexval()}">/{weight}pts</font>'
        )
    return "  &nbsp; &nbsp; ".join(parts)


# ─── COVER ─────────────────────────────────────────────────────────────────
def _cover_story(
    titulo_subtipo: str,
    nome_principal: str,
    subtitulo: str,
    tier: Optional[str],
    styles: dict,
) -> list:
    """Capa preta/dourada: logo, título, nome, subtítulo, tier badge, footer."""
    story = []
    logo = _logo_path()
    if logo:
        try:
            img = Image(str(logo), width=4 * cm, height=4 * cm * 0.4)
            img.hAlign = "LEFT"
            story.append(img)
        except Exception as e:
            log.warning("logo carga falhou: %s", e)
            story.append(Paragraph('<font color="%s"><b>WiNS Hub</b></font>' % DOURADO.hexval(), styles["h1_capa"]))
    else:
        story.append(Paragraph('<font color="%s"><b>WiNS Hub</b></font>' % DOURADO.hexval(), styles["h1_capa"]))
    story.append(Spacer(1, 12))
    # Linha dourada
    line_tbl = Table([[""]], colWidths=[A4[0] - 6 * cm], rowHeights=[2])
    line_tbl.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), DOURADO)]))
    story.append(line_tbl)
    story.append(Spacer(1, 18))

    story.append(Paragraph("Inteligência Comercial", styles["h2_capa"]))
    story.append(Paragraph(titulo_subtipo, styles["h1_capa"]))
    story.append(Spacer(1, 60))
    story.append(Paragraph(_escape(nome_principal), styles["obra_nome_capa"]))
    story.append(Paragraph(_escape(subtitulo), styles["obra_empresa_capa"]))
    story.append(Spacer(1, 30))

    if tier and tier in TIER_COLORS:
        c = TIER_COLORS[tier]
        badge = Table([[f"  {tier}  "]], colWidths=[2.5 * cm])
        badge.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), c),
            ("TEXTCOLOR", (0, 0), (-1, -1), PRETO),
            ("FONTNAME", (0, 0), (-1, -1), _FONT_BOLD),
            ("FONTSIZE", (0, 0), (-1, -1), 10),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ]))
        badge.hAlign = "LEFT"
        story.append(badge)
    story.append(Spacer(1, 180))
    story.append(Paragraph(
        f"Gerado em {datetime.now().strftime('%d/%m/%Y %H:%M')} &nbsp;|&nbsp; "
        f"Confidencial — WiNS Hub Comercial",
        styles["footer_capa"],
    ))
    return story


# ─── PDF OBRA ──────────────────────────────────────────────────────────────
def build_pdf_obra(
    obra: dict,
    decisores: list[dict],
    matches: list[dict],
    time_ideal: dict | None = None,
) -> bytes:
    """Gera PDF profissional do match de uma obra.

    obra: {id, nome, empresa, uf, municipio, setor, fase, valor_estimado, valor_formatado,
           lead_score, classificacao_computed, fonte, url_fonte, validacao_data, ...}
    decisores: [{nome, cargo, email, linkedin_url, tipo_cargo, ...}]
    matches: [{cnpj, razao_social, uf, porte_inferido, capital_social, cnae_principal,
               score, score_breakdown}]
    time_ideal: {categorias: [{nome, fornecedor: {razao_social, cnpj, score}, gap: bool}],
                 score_medio, cobertura_pct, gaps_count}
    """
    _register_fonts()
    styles = _build_styles()
    buf = io.BytesIO()
    doc = _new_doc(buf, f"Match — {obra.get('nome', 'Obra')}")
    story: list = []

    # ── Capa ──
    story.extend(_cover_story(
        titulo_subtipo="Match de Fornecedores",
        nome_principal=obra.get("nome") or "Obra",
        subtitulo=titulo_empresa(obra.get("empresa")) if obra.get("empresa") else "Empresa em validação",
        tier=obra.get("classificacao_computed"),
        styles=styles,
    ))
    story.append(NextPageTemplate("content"))
    story.append(PageBreak())

    # ── Pág 2: A OBRA ──
    story.append(_section_header("A OBRA", styles))
    story.append(Spacer(1, 14))

    capex_str = obra.get("valor_formatado") or format_capex(obra.get("valor_estimado"))
    grid_data = [
        [
            Paragraph("CAPEX", styles["kpi_label"]),
            Paragraph("FASE", styles["kpi_label"]),
            Paragraph("UF", styles["kpi_label"]),
        ],
        [
            Paragraph(capex_str, styles["kpi_value"] if obra.get("valor_estimado") else styles["kpi_value_dim"]),
            Paragraph(humanizar_fase(obra.get("fase")), styles["kpi_value_neutral"]),
            Paragraph(_escape(obra.get("uf") or "—"), styles["kpi_value_neutral"]),
        ],
        [
            Paragraph("SETOR", styles["kpi_label"]),
            Paragraph("LEAD SCORE", styles["kpi_label"]),
            Paragraph("FONTE", styles["kpi_label"]),
        ],
        [
            Paragraph(_escape((obra.get("setor") or "—").replace("_", " ").title()), styles["kpi_value_neutral"]),
            Paragraph(str(obra.get("lead_score") or "—"), styles["kpi_value"]),
            Paragraph(_escape(obra.get("fonte") or "—"), styles["kpi_value_neutral"]),
        ],
    ]
    col_w = (A4[0] - 3 * cm) / 3
    grid = Table(grid_data, colWidths=[col_w] * 3)
    grid.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("BACKGROUND", (0, 0), (-1, 0), HexColor("#f3f4f6")),
        ("BACKGROUND", (0, 2), (-1, 2), HexColor("#f3f4f6")),
    ]))
    story.append(grid)
    story.append(Spacer(1, 20))

    # ── Decisores ──
    story.append(_section_header("DECISOR", styles))
    story.append(Spacer(1, 8))
    if not decisores:
        story.append(Paragraph(
            '<font color="%s"><i>Decisor não mapeado</i></font>' % CINZA_DIM.hexval(),
            styles["body"],
        ))
    else:
        for dec in decisores[:5]:  # limita 5 pra não explodir página
            linhas: list[str] = []
            nome_part = f"<b>{_escape(dec.get('nome'))}</b>"
            cargo_part = _escape(dec.get("cargo") or "")
            linhas.append(f"{nome_part} &nbsp;|&nbsp; {cargo_part}")
            if dec.get("linkedin_url"):
                linhas.append(
                    f'<font color="#2563eb"><u>LinkedIn:</u></font> <link href="{_escape(dec["linkedin_url"])}">{_escape(dec["linkedin_url"])}</link>'
                )
            if dec.get("email"):
                email = _escape(dec["email"])
                verified = " <font color='%s'>✓</font>" % VERDE.hexval() if (dec.get("email_status") == "verified") else ""
                linhas.append(f'<link href="mailto:{email}"><font color="#2563eb"><u>{email}</u></font></link>{verified}')
            if dec.get("telefone"):
                linhas.append(f'Tel: {_escape(dec["telefone"])}')
            box = Table(
                [[Paragraph("<br/>".join(linhas), styles["body"])]],
                colWidths=[A4[0] - 3 * cm],
            )
            box.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), HexColor("#f9fafb")),
                ("LINEBEFORE", (0, 0), (0, -1), 2, DOURADO),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]))
            story.append(box)
            story.append(Spacer(1, 6))

    story.append(PageBreak())

    # ── Pág 3+: FORNECEDORES ──
    story.append(_section_header("FORNECEDORES COMPATÍVEIS — TOP 20", styles))
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        '<font color="%s"><i>Ordenados por score de compatibilidade técnica (engine v2)</i></font>' % CINZA_DIM.hexval(),
        styles["body_dim"],
    ))
    story.append(Spacer(1, 12))

    if not matches:
        story.append(Paragraph(
            '<font color="%s"><i>Nenhum fornecedor compatível encontrado. '
            'Execute o engine de matchmaking para esta obra.</i></font>' % CINZA_DIM.hexval(),
            styles["body"],
        ))
    else:
        for i, m in enumerate(matches[:20], start=1):
            nome = titulo_empresa(m.get("razao_social"))
            score = m.get("score") or 0
            cnpj = _escape(m.get("cnpj") or "")
            uf = _escape(m.get("uf") or "—")
            porte = _escape((m.get("porte_inferido") or "—").title())
            cap_str = format_capex(m.get("capital_social"))
            cnae = _escape(m.get("cnae_principal") or "—")

            breakdown_txt = _format_breakdown(m.get("score_breakdown"))

            header_row = [
                [
                    Paragraph(f"<b>{i:02d}. {_escape(nome)}</b>", styles["fornec_nome"]),
                    Paragraph(f"<b>{score:.0f}</b>", styles["score_grande"]),
                ],
                [
                    Paragraph(
                        f"CNPJ {cnpj} &nbsp;|&nbsp; {uf} &nbsp;|&nbsp; {porte} &nbsp;|&nbsp; "
                        f"Capital {cap_str} &nbsp;|&nbsp; CNAE {cnae}",
                        styles["fornec_meta"],
                    ),
                    "",
                ],
                [
                    Paragraph(breakdown_txt, styles["body_dim"]),
                    "",
                ],
            ]
            card = Table(header_row, colWidths=[(A4[0] - 3 * cm) * 0.78, (A4[0] - 3 * cm) * 0.22])
            bg = HexColor("#fafafa") if i % 2 == 0 else BRANCO
            card.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), bg),
                ("VALIGN", (0, 0), (-1, 0), "MIDDLE"),
                ("VALIGN", (0, 1), (-1, -1), "TOP"),
                ("SPAN", (0, 1), (1, 1)),
                ("SPAN", (0, 2), (1, 2)),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LINEBELOW", (0, -1), (-1, -1), 0.5, DOURADO_DIM),
            ]))
            story.append(KeepTogether(card))
            story.append(Spacer(1, 4))

    # ── TIME IDEAL ──
    if time_ideal and time_ideal.get("categorias"):
        story.append(PageBreak())
        story.append(_section_header("TIME IDEAL POR CATEGORIA", styles))
        story.append(Spacer(1, 12))

        # KPIs linha
        kpis_row = [[
            Paragraph("SCORE MÉDIO", styles["kpi_label"]),
            Paragraph("COBERTURA", styles["kpi_label"]),
            Paragraph("GAPS", styles["kpi_label"]),
        ], [
            Paragraph(f"{time_ideal.get('score_medio', 0):.0f}", styles["kpi_value"]),
            Paragraph(f"{time_ideal.get('cobertura_pct', 0):.0f}%", styles["kpi_value"]),
            Paragraph(str(time_ideal.get("gaps_count", 0)), styles["kpi_value"] if not time_ideal.get("gaps_count") else
                      ParagraphStyle("gap_kpi", parent=styles["kpi_value"], textColor=VERMELHO)),
        ]]
        kpis = Table(kpis_row, colWidths=[col_w] * 3)
        kpis.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), HexColor("#f3f4f6")),
            ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(kpis)
        story.append(Spacer(1, 14))

        # Tabela por categoria
        tbl_data = [[
            Paragraph("<b>Categoria</b>", styles["body"]),
            Paragraph("<b>Fornecedor</b>", styles["body"]),
            Paragraph("<b>Score</b>", styles["body"]),
            Paragraph("<b>Status</b>", styles["body"]),
        ]]
        gaps_list = []
        for cat in time_ideal["categorias"]:
            forn = cat.get("fornecedor") or {}
            is_gap = cat.get("gap") or not forn
            if is_gap:
                tbl_data.append([
                    Paragraph(_escape(cat.get("nome") or "—"), styles["body"]),
                    Paragraph('<font color="%s"><i>—</i></font>' % CINZA_DIM.hexval(), styles["body"]),
                    Paragraph('<font color="%s">—</font>' % CINZA_DIM.hexval(), styles["body"]),
                    Paragraph('<font color="%s"><b>GAP</b></font>' % VERMELHO.hexval(), styles["body"]),
                ])
                gaps_list.append(cat.get("nome") or "—")
            else:
                tbl_data.append([
                    Paragraph(_escape(cat.get("nome") or "—"), styles["body"]),
                    Paragraph(_escape(titulo_empresa(forn.get("razao_social"))), styles["body"]),
                    Paragraph(f'{forn.get("score", 0):.0f}', styles["body"]),
                    Paragraph('<font color="%s">✓ COBERTO</font>' % VERDE.hexval(), styles["body"]),
                ])
        tbl = Table(tbl_data, colWidths=[5 * cm, 6 * cm, 1.8 * cm, 2.5 * cm])
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), HexColor("#f3f4f6")),
            ("FONTNAME", (0, 0), (-1, 0), _FONT_BOLD),
            ("LINEBELOW", (0, 0), (-1, 0), 1, DOURADO),
            ("LINEBELOW", (0, 1), (-1, -1), 0.3, HexColor("#e5e7eb")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(tbl)

        if gaps_list:
            story.append(Spacer(1, 14))
            gaps_text = (
                f'<font color="{BRANCO.hexval()}"><b>Categorias sem fornecedor compatível:</b><br/>'
                + ", ".join(_escape(g) for g in gaps_list)
                + "<br/><br/><i>Sugestão: ampliar raio de busca UF ou flexibilizar critérios de match.</i></font>"
            )
            gaps_box = Table([[Paragraph(gaps_text, styles["body"])]], colWidths=[A4[0] - 3 * cm])
            gaps_box.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), VERMELHO),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                ("TOPPADDING", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ]))
            story.append(gaps_box)

    # ── Render ──
    doc.build(story)
    return buf.getvalue()


# ─── PDF FORNECEDOR ────────────────────────────────────────────────────────
def build_pdf_fornecedor(
    fornecedor: dict,
    obras: list[dict],
) -> bytes:
    """Gera PDF profissional do match inverso (fornecedor → obras).

    fornecedor: {cnpj, razao_social, nome_fantasia, situacao, capital_social, porte_inferido,
                 uf, municipio, data_fundacao, cnae_principal, cnae_principal_descricao,
                 cnae_secundarios}
    obras: [{obra_id, nome, empresa, valor_estimado, valor_formatado, fase, uf, setor,
             classificacao_computed, score, score_breakdown,
             decisor_nome, decisor_cargo, decisor_email, decisor_email_verificado}]
    """
    _register_fonts()
    styles = _build_styles()
    buf = io.BytesIO()
    doc = _new_doc(buf, f"Match — {titulo_empresa(fornecedor.get('razao_social', 'Fornecedor'))}")
    story: list = []

    raz = titulo_empresa(fornecedor.get("razao_social"))
    sub = (
        f"CNPJ {_escape(fornecedor.get('cnpj') or '—')} &nbsp;|&nbsp; "
        f"{_escape(fornecedor.get('uf') or '—')} &nbsp;|&nbsp; "
        f"{_escape((fornecedor.get('porte_inferido') or '—').title())} &nbsp;|&nbsp; "
        f"{_escape(fornecedor.get('situacao') or '—')}"
    )

    # ── Capa ──
    story.extend(_cover_story(
        titulo_subtipo="Obras Compatíveis",
        nome_principal=raz,
        subtitulo=sub,
        tier=None,
        styles=styles,
    ))
    story.append(NextPageTemplate("content"))
    story.append(PageBreak())

    # ── PERFIL ──
    story.append(_section_header("PERFIL DO FORNECEDOR", styles))
    story.append(Spacer(1, 14))

    situacao = fornecedor.get("situacao") or "—"
    situacao_color = VERDE if situacao.upper() == "ATIVA" else VERMELHO
    capital_str = format_capex(fornecedor.get("capital_social"))

    grid_data = [
        [
            Paragraph("RAZÃO SOCIAL", styles["kpi_label"]),
            Paragraph("CNPJ", styles["kpi_label"]),
        ],
        [
            Paragraph(_escape(raz), styles["kpi_value_neutral"]),
            Paragraph(_escape(fornecedor.get("cnpj") or "—"), styles["kpi_value_neutral"]),
        ],
        [
            Paragraph("SITUAÇÃO RFB", styles["kpi_label"]),
            Paragraph("DATA FUNDAÇÃO", styles["kpi_label"]),
        ],
        [
            Paragraph(f'<font color="{situacao_color.hexval()}"><b>{_escape(situacao)}</b></font>', styles["body"]),
            Paragraph(format_data(fornecedor.get("data_fundacao")), styles["kpi_value_neutral"]),
        ],
        [
            Paragraph("CAPITAL SOCIAL", styles["kpi_label"]),
            Paragraph("PORTE", styles["kpi_label"]),
        ],
        [
            Paragraph(capital_str, styles["kpi_value"] if fornecedor.get("capital_social") else styles["kpi_value_dim"]),
            Paragraph(_escape((fornecedor.get("porte_inferido") or "—").title()), styles["kpi_value_neutral"]),
        ],
        [
            Paragraph("UF", styles["kpi_label"]),
            Paragraph("MUNICÍPIO", styles["kpi_label"]),
        ],
        [
            Paragraph(_escape(fornecedor.get("uf") or "—"), styles["kpi_value_neutral"]),
            Paragraph(_escape(fornecedor.get("municipio") or "—"), styles["kpi_value_neutral"]),
        ],
    ]
    grid = Table(grid_data, colWidths=[(A4[0] - 3 * cm) / 2] * 2)
    grid.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("BACKGROUND", (0, 0), (-1, 0), HexColor("#f3f4f6")),
        ("BACKGROUND", (0, 2), (-1, 2), HexColor("#f3f4f6")),
        ("BACKGROUND", (0, 4), (-1, 4), HexColor("#f3f4f6")),
        ("BACKGROUND", (0, 6), (-1, 6), HexColor("#f3f4f6")),
    ]))
    story.append(grid)
    story.append(Spacer(1, 18))

    # CNAE box
    cnae_p = fornecedor.get("cnae_principal") or "—"
    cnae_desc = fornecedor.get("cnae_principal_descricao") or ""
    cnae_secs = fornecedor.get("cnae_secundarios") or []
    if isinstance(cnae_secs, str):
        cnae_secs = [c.strip() for c in cnae_secs.split(",") if c.strip()]
    cnae_secs_view = ", ".join(_escape(c) for c in cnae_secs[:5])
    if len(cnae_secs) > 5:
        cnae_secs_view += f" <i>+ {len(cnae_secs)-5} outros</i>"
    cnae_text = (
        f"<b>CNAE Principal:</b> {_escape(cnae_p)}"
        + (f" — {_escape(cnae_desc)}" if cnae_desc else "")
    )
    if cnae_secs_view:
        cnae_text += f"<br/><b>CNAEs Secundários:</b> {cnae_secs_view}"
    cnae_box = Table([[Paragraph(cnae_text, styles["body"])]], colWidths=[A4[0] - 3 * cm])
    cnae_box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), HexColor("#f9fafb")),
        ("LINEBEFORE", (0, 0), (0, -1), 2, DOURADO),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))
    story.append(cnae_box)
    story.append(PageBreak())

    # ── TOP 10 OBRAS ──
    story.append(_section_header("TOP 10 OBRAS COMPATÍVEIS", styles))
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        '<font color="%s"><i>Ordenadas por score de compatibilidade técnica (engine v2)</i></font>' % CINZA_DIM.hexval(),
        styles["body_dim"],
    ))
    story.append(Spacer(1, 12))

    if not obras:
        story.append(Paragraph(
            '<font color="%s"><i>Nenhuma obra compatível encontrada. '
            'Execute o engine de matchmaking para este fornecedor.</i></font>' % CINZA_DIM.hexval(),
            styles["body"],
        ))
    else:
        for i, o in enumerate(obras[:10], start=1):
            emp = titulo_empresa(o.get("empresa")) if o.get("empresa") else "Empresa em validação"
            uf = _escape(o.get("uf") or "—")
            setor = _escape((o.get("setor") or "—").replace("_", " ").title())
            capex = format_capex(o.get("valor_estimado"))
            fase = humanizar_fase(o.get("fase"))
            tier = o.get("classificacao_computed") or "—"
            tier_color = TIER_COLORS.get(tier, CINZA_DIM)
            score = o.get("score") or 0

            breakdown_txt = _format_breakdown(o.get("score_breakdown"))

            # Decisor inline — placeholder anderson_csv ('Contato Comercial XXXX')
            # eh mascarado pro nivel admin: transforma limitacao em CTA comercial.
            dec_lines = []
            dec_nome_raw = o.get("decisor_nome")
            dec_masked = bool(dec_nome_raw) and dec_nome_raw.strip().lower().startswith("contato comercial")
            if dec_nome_raw and dec_masked:
                dec_lines.append(
                    f'<font color="{DOURADO_DIM.hexval()}"><b>DECISOR:</b></font> '
                    f'<font color="{CINZA_DIM.hexval()}"><i>🔒 Decisor disponível — upgrade para desbloquear</i></font>'
                )
            elif dec_nome_raw:
                check = (
                    " <font color='%s'>✓</font>" % VERDE.hexval()
                    if o.get("decisor_email_verificado")
                    else ""
                )
                em_part = ""
                if o.get("decisor_email"):
                    em_part = (
                        f' &nbsp;|&nbsp; <link href="mailto:{_escape(o["decisor_email"])}">'
                        f'<font color="#2563eb"><u>{_escape(o["decisor_email"])}</u></font></link>{check}'
                    )
                dec_lines.append(
                    f'<font color="{DOURADO_DIM.hexval()}"><b>DECISOR:</b></font> '
                    f'<b>{_escape(dec_nome_raw)}</b>'
                    f' &nbsp;|&nbsp; {_escape(o.get("decisor_cargo") or "—")}'
                    f'{em_part}'
                )

            tier_html = f'<font color="{tier_color.hexval()}"><b>{_escape(tier)}</b></font>'
            meta_line = (
                f"{_escape(emp)} &nbsp;|&nbsp; {uf} &nbsp;|&nbsp; {setor}"
            )
            capex_line = f"{capex} &nbsp;|&nbsp; {fase} &nbsp;|&nbsp; {tier_html}"

            rows = [
                [
                    Paragraph(f"<b>{i:02d}. {_escape(o.get('nome') or '—')}</b>", styles["fornec_nome"]),
                    Paragraph(f"<b>{score:.0f}</b>", styles["score_grande"]),
                ],
                [Paragraph(meta_line, styles["fornec_meta"]), ""],
                [Paragraph(capex_line, styles["body_dim"]), ""],
                [Paragraph(breakdown_txt, styles["body_dim"]), ""],
            ]
            if dec_lines:
                rows.append([Paragraph("<br/>".join(dec_lines), styles["body_dim"]), ""])

            bg = HexColor("#fafafa") if i % 2 == 0 else BRANCO
            card = Table(rows, colWidths=[(A4[0] - 3 * cm) * 0.82, (A4[0] - 3 * cm) * 0.18])
            spans = [("SPAN", (0, r), (1, r)) for r in range(1, len(rows))]
            card.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), bg),
                ("VALIGN", (0, 0), (-1, 0), "MIDDLE"),
                ("VALIGN", (0, 1), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LINEBELOW", (0, -1), (-1, -1), 0.5, DOURADO_DIM),
                *spans,
            ]))
            story.append(KeepTogether(card))
            story.append(Spacer(1, 6))

    # ── OPORTUNIDADES ──
    story.append(PageBreak())
    story.append(_section_header("ANÁLISE DE OPORTUNIDADES", styles))
    story.append(Spacer(1, 14))

    # Helpers de filtragem — fases disponiveis no DB:
    # OPERACAO / PLANEJAMENTO / EM_EXECUCAO / LICITACAO_ABERTA /
    # LICENCA_INSTALACAO / LICENCA_PREVIA / PROJETO / CONCLUIDA.
    # Pipeline Quente: scores >= 70 em qualquer fase pre-operacao.
    # Acao Imediata: licitacao aberta; fallback p/ planejamento quando ausente.
    # Enriquecimento: score >= 65 + decisor faltando ou mascarado.
    _FASES_PRE_OP = {"EM_EXECUCAO", "PLANEJAMENTO", "LICENCA_INSTALACAO",
                     "LICENCA_PREVIA", "PROJETO"}
    _LICITACAO_FASES = {"LICITACAO_ABERTA", "LICITACAO_ABER"}

    def _decisor_mascarado(dec_nome: Optional[str]) -> bool:
        """True se decisor eh placeholder anderson_csv ('Contato Comercial XXXX')."""
        return bool(dec_nome) and dec_nome.strip().lower().startswith("contato comercial")

    licitando = [o for o in obras if o.get("fase") in _LICITACAO_FASES]
    if not licitando:
        # Fallback: planejamento + licenca previa (proxy de "antes da execucao")
        licitando = [o for o in obras
                     if o.get("fase") in ("PLANEJAMENTO", "LICENCA_PREVIA")]

    quentes = [o for o in obras
               if o.get("fase") in _FASES_PRE_OP and (o.get("score") or 0) >= 70]

    sem_decisor = [o for o in obras
                   if (o.get("score") or 0) >= 65
                   and (not o.get("decisor_nome") or _decisor_mascarado(o.get("decisor_nome")))]

    def _seccao(emoji_titulo: str, descricao: str, lista: list[dict], bg_color: HexColor) -> Table:
        if not lista:
            content = f"<i>Nenhuma obra nesta categoria.</i>"
        else:
            items = []
            for o in lista[:5]:
                dec_raw = o.get("decisor_nome")
                masked = _decisor_mascarado(dec_raw)
                # Em "Enriquecimento Sugerido" mostra CTA mascarado pro decisor placeholder
                items.append(
                    f"• <b>{_escape(o.get('nome', '—'))}</b> — "
                    f"{_escape(titulo_empresa(o.get('empresa')) if o.get('empresa') else 'Empresa em validação')} · "
                    f"score {o.get('score', 0):.0f}"
                )
                if masked:
                    items.append(
                        f'  <font color="{CINZA_DIM.hexval()}"><i>🔒 Decisor disponível — upgrade para desbloquear</i></font>'
                    )
            content = "<br/>".join(items)
            if len(lista) > 5:
                content += f"<br/><i>+ {len(lista)-5} outras…</i>"
        body = f"<b>{emoji_titulo}</b><br/><i>{descricao}</i><br/><br/>{content}"
        t = Table([[Paragraph(body, styles["body"])]], colWidths=[A4[0] - 3 * cm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), bg_color),
            ("LEFTPADDING", (0, 0), (-1, -1), 12),
            ("RIGHTPADDING", (0, 0), (-1, -1), 12),
            ("TOPPADDING", (0, 0), (-1, -1), 10),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ]))
        return t

    story.append(_seccao(
        "🟡 AÇÃO IMEDIATA — Obras em Licitação Aberta",
        "Contato recomendado nas próximas 48h. Quando licitação não existe,"
        " mostra obras em PLANEJAMENTO/LICENÇA PRÉVIA como alternativa imediata.",
        licitando, HexColor("#fef3c7"),
    ))
    story.append(Spacer(1, 8))
    story.append(_seccao(
        "🟢 PIPELINE QUENTE — Obras pré-operação com alta compatibilidade (score ≥ 70)",
        "Oportunidade de fornecimento ativa em fases Em Execução, Planejamento,"
        " Licença Instalação/Prévia ou Projeto. Apresentar portfólio + cases.",
        quentes, HexColor("#d1fae5"),
    ))
    story.append(Spacer(1, 8))
    story.append(_seccao(
        "🔴 ENRIQUECIMENTO SUGERIDO — Score ≥ 65, decisor faltando ou mascarado",
        "Potencial alto — decisor a ser identificado via Hunter, pesquisa manual"
        " ou upgrade do cliente pra desbloquear contato.",
        sem_decisor, HexColor("#fee2e2"),
    ))

    story.append(Spacer(1, 20))
    story.append(Paragraph(
        '<para alignment="center"><font color="%s">'
        'Gerado por <b>WiNS Hub Comercial</b> — winshubcomercial.com.br<br/>'
        'Para contratar este relatório ou acesso completo à plataforma, '
        'entre em contato com seu consultor WiNS Hub.</font></para>' % CINZA_DIM.hexval(),
        styles["body_dim"],
    ))

    doc.build(story)
    return buf.getvalue()
