#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Manual do USUÁRIO do WiNS Hub Comercial — com guia rápido, índice clicável,
bookmarks e capturas de tela reais. reportlab + multiBuild."""
import os, psycopg2
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
                                PageBreak, HRFlowable, ListFlowable, ListItem, Image)
from reportlab.platypus.tableofcontents import TableOfContents

DB = dict(host="db", port=5432, dbname="wins_hub", user="wins_app", password=os.getenv("DB_PASSWORD",""))
def q(s):
    c=psycopg2.connect(**DB).cursor(); c.execute(s); r=c.fetchall(); c.close(); return r
tot_vis=q("SELECT count(*) FROM obras WHERE visivel IS NOT FALSE")[0][0]
hoje="25/06/2026"; SH="/tmp/shots"

ss=getSampleStyleSheet()
AZUL=colors.HexColor("#0B3D67"); AZUL2=colors.HexColor("#1565a8"); COBRE=colors.HexColor("#b87333"); CINZA=colors.HexColor("#444")
ss.add(ParagraphStyle("Capa",fontName="Helvetica-Bold",fontSize=30,textColor=AZUL,alignment=TA_CENTER,leading=36))
ss.add(ParagraphStyle("Sub",fontName="Helvetica",fontSize=13,textColor=CINZA,alignment=TA_CENTER,leading=18))
ss.add(ParagraphStyle("H1",fontName="Helvetica-Bold",fontSize=15,textColor=AZUL,spaceBefore=14,spaceAfter=7,leading=19))
ss.add(ParagraphStyle("H2",fontName="Helvetica-Bold",fontSize=11.5,textColor=AZUL2,spaceBefore=9,spaceAfter=3,leading=15))
ss.add(ParagraphStyle("Body",parent=ss["BodyText"],fontName="Helvetica",fontSize=10.5,leading=15,spaceAfter=5))
ss.add(ParagraphStyle("Bull",parent=ss["Body"],leftIndent=12,spaceAfter=2))
ss.add(ParagraphStyle("Dica",parent=ss["Body"],fontSize=9.5,textColor=CINZA,leftIndent=8,spaceBefore=2))
ss.add(ParagraphStyle("Cap",fontName="Helvetica-Oblique",fontSize=9,textColor=CINZA,alignment=TA_CENTER,spaceBefore=3,spaceAfter=8))
def P(t,s="Body"): return Paragraph(t,ss[s])
def bullets(items): return ListFlowable([ListItem(P(t,"Bull"),leftIndent=8) for t in items],bulletType="bullet",start="•",bulletColor=AZUL2)
def passos(rows):
    d=[[P(f"<b>{i+1}</b>","Body"),P(t,"Body")] for i,t in enumerate(rows)]
    tb=Table(d,colWidths=[0.9*cm,15.1*cm]); tb.setStyle(TableStyle([("VALIGN",(0,0),(-1,-1),"TOP"),("TOPPADDING",(0,0),(-1,-1),4),
      ("BOTTOMPADDING",(0,0),(-1,-1),4),("BACKGROUND",(0,0),(0,-1),colors.HexColor("#eef3f8")),("LINEBELOW",(0,0),(-1,-2),0.3,colors.HexColor("#e3e3e3"))])); return tb
def dica(t):
    tb=Table([[P("&#9733;","Body"),P(f"<b>Dica:</b> {t}","Dica")]],colWidths=[0.8*cm,15.2*cm])
    tb.setStyle(TableStyle([("VALIGN",(0,0),(-1,-1),"TOP"),("BACKGROUND",(0,0),(-1,-1),colors.HexColor("#fff8ee")),
      ("BOX",(0,0),(-1,-1),0.4,COBRE),("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5),("LEFTPADDING",(0,0),(-1,-1),6)])); return tb
def img(nome,legenda,w=15.0):
    p=f"{SH}/{nome}.png"
    if not os.path.exists(p): return [Spacer(1,1)]
    h=w*1800/2560
    im=Image(p,width=w*cm,height=h*cm); im.hAlign="CENTER"
    box=Table([[im]],colWidths=[w*cm]); box.setStyle(TableStyle([("BOX",(0,0),(-1,-1),0.6,colors.HexColor("#cccccc")),("TOPPADDING",(0,0),(-1,-1),3),("BOTTOMPADDING",(0,0),(-1,-1),3)]))
    return [Spacer(1,0.2*cm), box, P(legenda,"Cap")]

class ManualDoc(SimpleDocTemplate):
    def afterFlowable(self, fl):
        if fl.__class__.__name__=="Paragraph" and fl.style.name=="H1":
            txt=fl.getPlainText()
            key="h_"+"".join(c for c in txt if c.isalnum())[:24]  # estável entre passadas
            self.canv.bookmarkPage(key); self.canv.addOutlineEntry(txt,key,0,0)
            self.notify("TOCEntry",(0,txt,self.page,key))

E=[]
# CAPA
E+=[Spacer(1,4.3*cm),P("WiNS Hub Comercial","Capa"),Spacer(1,0.4*cm),
    P("Manual do Usuário","Sub"),P("Como usar a plataforma, tela por tela","Sub"),
    Spacer(1,0.3*cm),P(f"{tot_vis:,} obras ativas &bull; atualizado em {hoje}".replace(",","."),"Sub"),PageBreak()]

# GUIA RÁPIDO (1 página)
E+=[P("Guia Rápido (comece por aqui)","H1"),
  P("Em 5 passos você já está usando o essencial:"),
  passos(["<b>Entre</b> com seu e-mail e senha (ou clique em “Começar grátis” para criar a conta).",
          "Abra a aba <b>Matches</b> ou clique em <b>“Suas Obras”</b> — você vê as obras do seu setor.",
          "Clique numa obra para abrir a <b>ficha completa</b> (decisores, valor, cadeia de fornecimento).",
          "Para ver os <b>contatos do decisor</b>, assine um plano (ou use “Buscar Decisor live”).",
          "Complete seu <b>Perfil Técnico</b> em “Minha conta” para melhorar os matches."]),
  dica("Os 4 selos: Ouro = decisor verificado (pronto para abordar); Prata = decisor parcial; Bronze = sem decisor ainda; Pipeline = aguardando a empresa vencedora da licitação."),
  *img("03_app","A página inicial: KPIs por tier, gráficos por setor e o botão “Começar grátis”."),
  PageBreak()]

# ÍNDICE
toc=TableOfContents()
toc.levelStyles=[ParagraphStyle("TOC1",fontName="Helvetica",fontSize=11,leftIndent=6,firstLineIndent=-6,spaceBefore=5,leading=16,textColor=AZUL2)]
E+=[P("Índice","H1"),P("(clique em um item para ir direto à seção)","Dica"),Spacer(1,0.2*cm),toc,PageBreak()]

# 1
E+=[P("1. Como acessar a plataforma","H1"),
  *img("01_landing","Tela de login: e-mail, senha, “Esqueci minha senha” e “Começar grátis”."),
  P("Criar sua conta (primeiro acesso)","H2"),
  passos(["Na página inicial, clique em <b>“Começar grátis”</b> (topo à direita ou no centro).",
          "Preencha seus dados e o <b>CNPJ</b> da sua empresa (apenas números).",
          "Confirme. Você entra no plano <b>Gratuito</b> (vê a vitrine; contatos ficam bloqueados)."]),
  dica("Cadastre o CNAE/CNPJ logo no início — é assim que a ferramenta filtra “as obras que combinam com você”."),
  P("Entrar (login)","H2"),
  passos(["Clique em <b>“Entrar”</b> no topo.","Digite <b>e-mail</b> e <b>senha</b>.","Clique em <b>“Entrar”</b> — você cai no painel principal."]),
  P("Esqueci minha senha","H2"),
  passos(["Na tela de login, clique em <b>“Esqueci minha senha →”</b>.","Digite o e-mail e clique em <b>“Enviar link”</b>.",
          "Abra o e-mail e clique no link.","Digite a <b>nova senha</b> (mín. 8), confirme e clique em <b>“Redefinir senha”</b>."])]

# 2
E+=[PageBreak(),P("2. A tela inicial: barra superior e menu lateral","H1"),
  P("Barra superior (topo)","H2"),
  bullets(["<b>Logo WiNS Hub</b> (esquerda): volta para a inicial.","<b>Créditos</b>: saldo em R$ para buscas avulsas.",
           "<b>Seu plano</b>: Gratuito / Setor / Nacional / Enterprise.","<b>Sino</b> 🔔: obras novas (capítulo 10).",
           "<b>Avatar</b> (suas iniciais): abre <b>Minha conta</b>."]),
  P("Menu lateral (esquerda) — “Explorar”","H2"),
  bullets(["<b>Obras</b> — todas as oportunidades.","<b>Matches</b> — obras compatíveis com você (nota 0–100).",
           "<b>Fornecedores</b> — base de empresas.","<b>Painel Admin</b> — só para administradores."]),
  dica("Obras, Matches e Fornecedores são o coração da ferramenta — você alterna entre elas pelo menu lateral.")]

# 3
E+=[P("3. Aba “Obras” — encontrar oportunidades","H1"),
  P("Lista de todas as obras, cada uma em um cartão."),
  P("Buscar e filtrar","H2"),
  passos(["Use a <b>caixa de busca</b> (“Buscar empresa, setor…”).","Clique em <b>“Filtros”</b> para refinar:",
          "<b>Ordenar</b>: Recentes, Maior/Menor CAPEX, A-Z, Z-A.","<b>Tier</b>: Ouro, Prata, Bronze, Pipeline.",
          "<b>CAPEX</b>: faixas de valor.","<b>Setor</b> e <b>UF</b>: marque os de interesse.","Para zerar, use <b>“Limpar Filtros”</b>."]),
  P("Botão “Suas Obras” ⚡","H2"),
  P("Filtra automaticamente as obras do <b>seu setor</b> (pelo CNAE) e resume: quantas do seu setor, quantas com decisor e "
    "quantas novas (7 dias). As do seu segmento ganham a faixa <b>“⚡ DEMANDA DO SEU SETOR”</b>."),
  P("O cartão da obra","H2"),
  bullets(["<b>Selo de tier</b>, <b>Setor · UF</b>, <b>CAPEX</b>, <b>Empresa</b> e <b>Fase</b>.",
           "<b>Decisor</b>: bloqueado (🔒) no Gratuito; com plano, mostra nome, cargo e botões de LinkedIn/E-mail/Telefone."]),
  passos(["Clique no cartão para abrir a <b>ficha completa</b> (capítulo 6).","No fim da lista, use <b>“Carregar mais”</b>."])]

# 4
E+=[PageBreak(),P("4. Aba “Matches” — obras compatíveis com você","H1"),
  P("Igual à aba Obras, mas só com obras compatíveis, cada uma com <b>nota de 0 a 100</b> (faixa amarela no cartão)."),
  passos(["Abra <b>“Matches”</b> no menu.","As obras vêm ordenadas por compatibilidade.",
          "Clique na seta da faixa para ver o <b>detalhamento</b> (CNAE, UF, CAPEX, tier).",
          "Se aparecer “Nenhum match ainda”, use <b>“Regenerar matches”</b> ou complete o perfil técnico."]),
  dica("Matches são recalculados toda madrugada. Perfil Técnico mais completo = matches melhores.")]

# 5
E+=[P("5. Aba “Fornecedores” — pesquisar empresas","H1"),
  passos(["Abra <b>“Fornecedores”</b>.","Busque por <b>razão social, CNPJ ou CNAE</b>.",
          "Filtre por <b>CNAE</b>, <b>Porte</b> (MEI/PME/Média/Grande), <b>UF</b> e proximidade.",
          "Cada cartão traz nota, razão social, CNPJ, nº de matches, porte, CNAE e contatos."])]

# 6
E+=[PageBreak(),P("6. A ficha da obra (a tela mais importante)","H1"),
  P("Ao clicar numa obra abre a ficha completa (botão <b>“← Voltar”</b> no topo). Estrutura:"),
  P("Ciclo completo","H2"),
  P("Quatro blocos clicáveis: 🏗️ <b>Dono + Decisores</b> · ⚙️ <b>Executores</b> · 📦 <b>Tipos de Insumo</b> · 🏭 <b>Fornecedores</b>."),
  P("Informações principais","H2"),
  bullets(["Título, selo de tier, setor, UF, empresa.","<b>KPIs</b>: CAPEX, Fase, Lead score.","<b>Sobre a obra</b>: resumo automático."]),
  P("Decisor","H2"),
  bullets(["<b>Gratuito</b>: contatos bloqueados (🔒) — clique em <b>“Ver Planos”</b>.","<b>Com plano</b>: nome, cargo e botões LinkedIn/E-mail/Telefone."]),
  P("Sem decisor? Clique em <b>“Buscar Decisor (live · R$ 10)”</b> — busca em tempo real e <b>só cobra se encontrar</b>."),
  P("Registrar contato / Pitch","H2"),
  P("Use <b>“Registrar contato”</b> após falar com o decisor (rende créditos). Use <b>“Gerar pitch comercial”</b> para um texto de abordagem em nome da sua empresa."),
  P("Impacto Econômico / Cadeia de Fornecimento","H2"),
  bullets(["<b>Impacto</b> (obras Ouro): produção gerada, PIB e empregos (base IBGE/Leontief).",
           "<b>Cadeia</b>: insumos que a obra demanda e os fornecedores de cada um."])]

# 7
E+=[PageBreak(),P("7. Minha conta (perfil)","H1"),
  P("Acesse pelo <b>avatar</b> (canto superior direito)."),
  P("Match Automático (R$ 10)","H2"),
  passos(["Veja seu <b>saldo de créditos</b>.","Clique em <b>“Buscar Obras Compatíveis · R$ 10,00”</b>.",
          "Recebe até 10 obras compatíveis, com nota e a razão do match.","Clique para abrir cada uma."]),
  dica("Complete antes o Perfil Técnico para o Match Automático funcionar."),
  P("Perfil Técnico","H2"),
  passos(["Clique em <b>“Completar/Editar perfil”</b>.","Preencha <b>CNAEs</b>, <b>UFs</b> e <b>CAPEX esperado</b> (obrigatórios) + serviços.","Clique em <b>“Salvar”</b>."]),
  P("Empresas vinculadas / Convite","H2"),
  bullets(["Adicione empresas pelo <b>CNPJ</b> (Própria ou Representada).",
           "Use seu <b>link de convite</b>: ganhe <b>R$ 20</b> em créditos por convidado que confirmar um decisor."])]

# 8
E+=[PageBreak(),P("8. Ranking Megaobras e relatórios por e-mail","H1"),
  *img("02_ranking","Ranking público das maiores obras por CAPEX, com opção de receber o PDF completo."),
  bullets(["Tabela com posição, obra, empresa, setor, UF, fase e CAPEX (clique numa linha para abrir).",
           "Topo: CAPEX total, nº de obras e setores."]),
  P("Receber o PDF / Newsletter","H2"),
  passos(["Preencha o <b>e-mail</b> e clique em <b>“Enviar PDF pro meu email”</b>.",
          "Para alertas, informe o e-mail e clique em <b>“Assinar”</b> (avisa quando entra obra de R$ 500mi+)."])]

# 9
E+=[P("9. Planos e como desbloquear contatos","H1"),
  P("No <b>Gratuito</b> você navega pela vitrine, mas os contatos ficam bloqueados. Para liberar, clique em <b>“Ver Planos”</b> e escolha:"),
  bullets(["<b>Setor — R$ 497/mês</b>: obras e fornecedores do seu setor.",
           "<b>Nacional — R$ 1.497/mês</b>: todas as obras + cadeia completa.",
           "<b>Enterprise</b>: relatórios de impacto, API e dados sob medida (sob consulta)."]),
  P("Após assinar, contatos, cadeia e relatórios aparecem liberados automaticamente.")]

# 10
E+=[P("10. Alertas de obras novas","H1"),
  passos(["Clique no <b>sino</b> 🔔 (o número = obras novas).","Veja a lista das últimas obras e clique para abrir.",
          "Use <b>“Marcar como vistas”</b> para limpar o contador.",
          "Em <b>“Configurar preferências”</b>: ative e-mail, escolha a frequência e a nota mínima."])]

# 11
E+=[PageBreak(),P("11. Dicas rápidas","H1"),
  bullets(["Comece pelo botão <b>“Suas Obras”</b> ou pela aba <b>Matches</b>.",
           "Perfil Técnico completo = matches melhores.",
           "<b>Ouro</b> = decisor pronto; <b>Pipeline</b> = aguardando vencedor da licitação.",
           "Use <b>“Registrar contato”</b> sempre — organiza e rende créditos.",
           "Indique colegas pelo <b>link de convite</b> e acumule créditos."]),
  Spacer(1,0.5*cm),HRFlowable(width="100%",color=AZUL2),
  P(f"<i>WiNS Hub Comercial — Manual do Usuário · atualizado em {hoje}.</i>","Dica")]

def rod(c,d):
    c.saveState(); c.setFont("Helvetica",8); c.setFillColor(CINZA)
    c.drawString(2*cm,1.1*cm,"WiNS Hub Comercial — Manual do Usuário")
    c.drawRightString(19*cm,1.1*cm,f"Página {d.page}"); c.restoreState()

doc=ManualDoc("/tmp/manual_winshub.pdf",pagesize=A4,leftMargin=2*cm,rightMargin=2*cm,topMargin=2*cm,bottomMargin=1.8*cm,
              title="WiNS Hub Comercial — Manual do Usuário",author="WiNS Hub")
doc.multiBuild(E,onFirstPage=rod,onLaterPages=rod)
print("PDF gerado (multiBuild com índice)")
