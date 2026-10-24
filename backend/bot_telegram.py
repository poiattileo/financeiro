#!/usr/bin/env python3
"""
Bot Telegram para o app de Financas.
Roda em loop continuo (long polling), sem precisar de HTTPS/dominio.

Formato de mensagem aceito (linhas separadas por ENTER):
  Linha 1: Nome do gasto fixo OU descricao de um lancamento avulso
  Linha 2: Valor (aceita virgula ou ponto, com ou sem sinal)
  Linha 3 (opcional): Mes (ex: "agosto", "08/2026", ou vazio = mes atual)
  Linha 4 (opcional): Motivo/comentario (fica salvo e aparece no tooltip do site)

Se a Linha 1 bater com o nome de um gasto fixo ja cadastrado (do usuario
vinculado aquele chat_id), o valor eh aplicado como AJUSTE (soma/subtrai)
daquele gasto. Digite com sinal: -35.42 para subtrair, +35.42 ou 35.42
para somar.

Se nao bater com nenhum gasto fixo, cria um LANCAMENTO AVULSO novo com
essa descricao e o valor (sempre tratado como positivo/gasto).

Comandos:
  /start           - mensagem de boas-vindas
  /vincular CODIGO - vincula este chat ao usuario que gerou o CODIGO no site
  /desvincular     - remove o vinculo
  /ajuda           - mostra o formato de mensagem
  /comandos        - lista todos os comandos
  /lancar          - registro guiado (rapido ou parcelado, tudo em 1 mensagem)
  /reverter        - cancela um lançamento (desfaz do fixo e apaga o registro)
  /receber         - marcar cobrança como paga por botões (conta → pessoa → mês)
  /relatorio       - relatorio estilo WhatsApp (pergunta gasto fixo + mes)
  /relatorios      - relatorios rapidos do mes
  /fixos           - lista todos os gastos fixos (nome + valor do mes)
  /cancelar        - cancela a conversa atual
"""
import os
import re
import time
import unicodedata
import uuid
import requests
import mysql.connector
from datetime import datetime

# ── Carrega .env manualmente (sem dependencias extras) ──
ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")
env = {}
with open(ENV_PATH) as f:
    for line in f:
        line = line.strip()
        if line and "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k] = v

BOT_TOKEN = env.get("TELEGRAM_BOT_TOKEN", "")
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
UPLOAD_DIR = env.get("UPLOAD_DIR", "/opt/financeiro/uploads")

LEMBRETE_HORAS_INATIVO = 24

MESES_PT = {
    "janeiro":1,"fevereiro":2,"marco":3,"março":3,"abril":4,"maio":5,"junho":6,
    "julho":7,"agosto":8,"setembro":9,"outubro":10,"novembro":11,"dezembro":12,
    "jan":1,"fev":2,"mar":3,"abr":4,"mai":5,"jun":6,"jul":7,"ago":8,"set":9,"out":10,"nov":11,"dez":12
}

def get_db():
    return mysql.connector.connect(
        host=env.get("DB_HOST","localhost"),
        user=env.get("DB_USER","financeiro"),
        password=env.get("DB_PASS",""),
        database=env.get("DB_NAME","financeiro"),
        autocommit=True
    )

def normalizar(s):
    s = s.strip().lower()
    s = ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')
    return s

def enviar_msg(chat_id, texto, teclado=None):
    try:
        payload = {"chat_id": chat_id, "text": texto, "parse_mode": "Markdown"}
        if teclado:
            payload["reply_markup"] = {"inline_keyboard": teclado}
        requests.post(f"{API_URL}/sendMessage", json=payload, timeout=10)
    except Exception as e:
        print(f"[erro ao enviar] {e}")

def responder_callback(callback_id):
    try:
        requests.post(f"{API_URL}/answerCallbackQuery", json={"callback_query_id": callback_id}, timeout=10)
    except Exception:
        pass

def buscar_usuario_por_chat(chat_id):
    db = get_db(); cur = db.cursor(dictionary=True)
    cur.execute("SELECT id, username, mes_inicio FROM usuarios WHERE telegram_chat_id=%s", (str(chat_id),))
    row = cur.fetchone(); cur.close(); db.close()
    return row

def vincular_codigo(chat_id, codigo):
    db = get_db(); cur = db.cursor(dictionary=True)
    cur.execute("SELECT id, username FROM usuarios WHERE telegram_link_code=%s", (codigo,))
    row = cur.fetchone()
    if not row:
        cur.close(); db.close()
        return None
    cur.execute("UPDATE usuarios SET telegram_chat_id=%s, telegram_link_code=NULL WHERE id=%s",
        (str(chat_id), row["id"]))
    cur.close(); db.close()
    return row["username"]

def desvincular(chat_id):
    db = get_db(); cur = db.cursor()
    cur.execute("UPDATE usuarios SET telegram_chat_id=NULL WHERE telegram_chat_id=%s", (str(chat_id),))
    cur.close(); db.close()

def idx_do_mes(mes_inicio_str, ano, mes):
    if not mes_inicio_str:
        return 0
    y0, m0 = map(int, mes_inicio_str.split("-"))
    return (ano - y0) * 12 + (mes - m0)

def idx_hoje(mes_inicio_str):
    n = datetime.now()
    return idx_do_mes(mes_inicio_str, n.year, n.month)

# Mês de trabalho = calendário +1 (em setembro já se planeja outubro).
OFFSET_MES_TRABALHO = 1
def idx_trabalho(mes_inicio_str):
    return idx_hoje(mes_inicio_str) + OFFSET_MES_TRABALHO

def parse_mes(texto, mes_inicio_str):
    """Retorna o indice do mes a partir do texto (nome do mes ou MM/AAAA), ou None se vazio."""
    texto = texto.strip()
    if not texto:
        return None
    # formato MM/AAAA ou MM-AAAA
    m = re.match(r'^(\d{1,2})[/\-](\d{4})$', texto)
    if m:
        mes, ano = int(m.group(1)), int(m.group(2))
        return idx_do_mes(mes_inicio_str, ano, mes)
    # nome do mes (assume ano atual, ou proximo ano se mes ja passou muito)
    nome = normalizar(texto)
    for chave, num in MESES_PT.items():
        if nome.startswith(chave):
            ano = datetime.now().year
            # se o mes calculado for muito anterior ao atual, assume ano seguinte
            idx_candidato = idx_do_mes(mes_inicio_str, ano, num)
            if idx_candidato < idx_trabalho(mes_inicio_str) - 6:
                idx_candidato = idx_do_mes(mes_inicio_str, ano+1, num)
            return idx_candidato
    return None

def parse_valor(texto):
    """Extrai valor numerico (aceita virgula, ponto, sinal)."""
    texto = texto.strip().replace("R$","").replace(" ","")
    texto = texto.replace(".", "").replace(",", ".") if texto.count(",")==1 and texto.count(".")<=1 and "," in texto else texto.replace(",", ".")
    try:
        return float(texto)
    except ValueError:
        return None

# Convenção: NEGATIVO = a pagar, POSITIVO = a receber.
def fbrs(v):
    """Formata com sinal: -R$ X (pagar) | +R$ X (a receber)."""
    s = f"{abs(v):.2f}".replace(".", ",")
    if v > 0:
        return f"+R$ {s} (a receber)"
    if v < 0:
        return f"-R$ {s}"
    return "R$ 0,00"

def fnum(v):
    """Formata numero com virgula (sem R$), mantendo o sinal."""
    return f"{float(v):.2f}".replace(".", ",")

def aplicar_ajuste(atual, delta_typed):
    """delta negativo (desconto) aproxima de zero; positivo (acréscimo) afasta.
    Vale p/ dívida (negativo) e crédito (positivo)."""
    atual = float(atual or 0)
    delta_typed = float(delta_typed or 0)
    if delta_typed < 0:
        mag = max(0.0, round(abs(atual) - abs(delta_typed), 2))
        if mag == 0:
            return 0.0
        return -mag if atual < 0 else (mag if atual > 0 else 0.0)
    mag = round(abs(atual) + abs(delta_typed), 2)
    if atual == 0:
        return -mag  # assume dívida (caso comum de fixo)
    return -mag if atual < 0 else mag

def desfazer_ajuste(atual, tipo, v):
    """Reverso do aplicar_ajuste (p/ /reverter)."""
    atual = float(atual or 0)
    v = abs(float(v or 0))
    if tipo == "subtrair":  # desfaz desconto: afasta de zero
        mag = round(abs(atual) + v, 2)
        if atual == 0:
            return -mag
        return -mag if atual < 0 else mag
    mag = max(0.0, round(abs(atual) - v, 2))  # desfaz acréscimo: aproxima
    if mag == 0:
        return 0.0
    return -mag if atual < 0 else (mag if atual > 0 else 0.0)

def calcular_lancamento(user, linhas):
    """Valida as linhas do rápido e monta o pendente SEM salvar.
    Retorna (pendente, erro). pendente['kind'] = 'ajuste' | 'avulso'."""
    nome_ou_desc = linhas[0].strip()
    valor_raw = linhas[1].strip() if len(linhas) > 1 else ""
    mes_raw = linhas[2].strip() if len(linhas) > 2 else ""
    motivo_raw = linhas[3].strip() if len(linhas) > 3 else ""

    valor = parse_valor(valor_raw)
    if valor is None:
        return None, "❌ Não consegui entender o valor. Use algo como `-35.42` ou `78,90`."

    mes_inicio = user.get("mes_inicio") or ""
    idx = parse_mes(mes_raw, mes_inicio)
    if idx is None:
        idx = idx_trabalho(mes_inicio)

    db = get_db(); cur = db.cursor(dictionary=True)
    cur.execute("SELECT id, nome, cat FROM gastos WHERE user_id=%s", (user["id"],))
    gastos = cur.fetchall()

    nome_norm = normalizar(nome_ou_desc)
    gasto_match = None
    for g in gastos:
        if normalizar(g["nome"]) == nome_norm:
            gasto_match = g
            break

    if gasto_match:
        cur.execute("SELECT valor FROM gasto_valores WHERE gasto_id=%s AND idx=%s", (gasto_match["id"], idx))
        row = cur.fetchone()
        cur.close(); db.close()
        valor_atual = float(row["valor"]) if row else 0.0
        novo_valor = aplicar_ajuste(valor_atual, valor)
        op = "Desconto" if valor < 0 else "Acréscimo"
        tipo_aj = "subtrair" if valor < 0 else "somar"
        motivo_final = motivo_raw if motivo_raw else "via Telegram"
        return {"kind": "ajuste", "gasto_id": gasto_match["id"], "gasto_nome": gasto_match["nome"],
                "cat": gasto_match["cat"], "idx": idx, "valor": valor, "valor_abs": abs(valor),
                "anterior": valor_atual, "novo": novo_valor, "op": op, "tipo_aj": tipo_aj,
                "motivo": motivo_final, "motivo_raw": motivo_raw,
                "desc": f"{op}: {gasto_match['nome']}"}, None
    cur.close(); db.close()
    motivo_final = motivo_raw if motivo_raw else "via Telegram"
    return {"kind": "avulso", "desc": nome_ou_desc, "valor_abs": abs(valor),
            "idx": idx, "motivo": motivo_final, "motivo_raw": motivo_raw}, None

def executar_lancamento(user, p):
    """Salva o pendente do rápido. Retorna (lancamento_id, texto)."""
    db = get_db(); cur = db.cursor()
    if p["kind"] == "ajuste":
        if p["novo"] != 0:
            cur.execute("INSERT INTO gasto_valores (gasto_id,idx,valor) VALUES (%s,%s,%s) ON DUPLICATE KEY UPDATE valor=%s",
                (p["gasto_id"], p["idx"], p["novo"], p["novo"]))
        else:
            cur.execute("DELETE FROM gasto_valores WHERE gasto_id=%s AND idx=%s", (p["gasto_id"], p["idx"]))
        cur.execute(
            "INSERT INTO lancamentos (user_id,descricao,valor,cat,local_nome,recorrencia,motivo,mes_idx,tipo_ajuste,gasto_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (user["id"], p["desc"], p["valor_abs"], p["cat"], "", "nunca", p["motivo"], p["idx"], p["tipo_aj"], p["gasto_id"])
        )
        lid = cur.lastrowid
        cur.close(); db.close()
        sinal = "a receber" if p["novo"] > 0 else "a pagar"
        texto = (f"✅ *{p['gasto_nome']}* ajustado!\n"
                f"Valor anterior: {fbrs(p['anterior'])}\n"
                f"Ajuste: {'−' if p['valor']<0 else '+'} R$ {p['valor_abs']:.2f}\n"
                f"*Novo valor: {fbrs(p['novo'])}* ({sinal})")
        if p["motivo_raw"]:
            texto += f"\n💬 _{p['motivo_raw']}_"
        return lid, texto
    cur.execute(
        "INSERT INTO lancamentos (user_id,descricao,valor,cat,local_nome,recorrencia,motivo,mes_idx) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (user["id"], p["desc"], p["valor_abs"], "outros", "", "nunca", p["motivo"], p["idx"])
    )
    lid = cur.lastrowid
    cur.close(); db.close()
    texto = f"✅ Lançamento avulso registrado!\n*{p['desc']}* — R$ {p['valor_abs']:.2f}"
    if p["motivo_raw"]:
        texto += f"\n💬 _{p['motivo_raw']}_"
    return lid, texto

def processar_lancamento(user, linhas):
    # mensagem direta (fora do /lancar): calcula e já salva, como antes
    p, err = calcular_lancamento(user, linhas)
    if err:
        return err
    _, texto = executar_lancamento(user, p)
    return texto

# ── RELATÓRIOS ──
def calc_totais_mes(user, idx):
    db = get_db(); cur = db.cursor(dictionary=True)
    cur.execute("SELECT salario FROM usuarios WHERE id=%s", (user["id"],))
    sal = float(cur.fetchone()["salario"] or 0)

    cur.execute("SELECT id, nome, cat FROM gastos WHERE user_id=%s", (user["id"],))
    gastos = cur.fetchall()
    total_fixos = 0.0
    detalhes_fixos = []
    for g in gastos:
        cur.execute("SELECT valor FROM gasto_valores WHERE gasto_id=%s AND idx=%s", (g["id"], idx))
        row = cur.fetchone()
        v = float(row["valor"]) if row else 0.0
        if v != 0:
            total_fixos += v
            detalhes_fixos.append((g["nome"], v))

    cur.execute("SELECT nome, total, qtd, mes_idx, valor_parcela FROM parcelas WHERE user_id=%s", (user["id"],))
    total_parc = 0.0
    for p in cur.fetchall():
        k = idx - p["mes_idx"]
        if 0 <= k < p["qtd"]:
            total_parc += float(p["valor_parcela"])

    # Lançamentos NÃO somam no total (só informam — o valor já está no fixo).
    # Total = fixos + parcelas. A lista abaixo serve só p/ conferência.
    cur.execute("SELECT descricao, valor FROM lancamentos WHERE user_id=%s AND mes_idx=%s", (user["id"], idx))
    total_lanc = 0.0
    detalhes_lanc = []
    for l in cur.fetchall():
        total_lanc += float(l["valor"])
        detalhes_lanc.append((l["descricao"], float(l["valor"])))

    cur.close(); db.close()
    total = round(total_fixos + total_parc, 2)
    sobra = round(sal + total, 2)  # fixos/parcelas negativos = a pagar
    return {
        "sal": sal, "fixos": round(total_fixos,2), "parcelas": round(total_parc,2),
        "lancamentos": round(total_lanc,2), "total": total, "sobra": sobra,
        "detalhes_fixos": detalhes_fixos, "detalhes_lanc": detalhes_lanc
    }

def relatorio_resumo_mes(user):
    idx = idx_trabalho(user.get("mes_inicio") or "")
    d = calc_totais_mes(user, idx)
    mes_nome = datetime.now().strftime("%B/%Y")
    txt = (f"📊 *Resumo — {mes_nome}*\n\n"
           f"💵 Salário: R$ {d['sal']:.2f}\n"
           f"🏠 Fixos: {fbrs(d['fixos'])}\n"
           f"💳 Parcelas: {fbrs(d['parcelas'])}\n"
           f"⚡ Lançamentos: R$ {d['lancamentos']:.2f}\n"
           f"➖➖➖➖➖➖➖\n"
           f"💸 Total: {fbrs(d['total'])}\n"
           f"{'✅' if d['sobra']>=0 else '⚠️'} *Sobra: {fbrs(d['sobra'])}*")
    return txt

def relatorio_gastos_fixos(user):
    idx = idx_trabalho(user.get("mes_inicio") or "")
    d = calc_totais_mes(user, idx)
    if not d["detalhes_fixos"]:
        return "📋 Nenhum gasto fixo com valor neste mês."
    linhas = "\n".join([f"• {nome}: {fbrs(v)}" for nome, v in sorted(d["detalhes_fixos"], key=lambda x:-abs(x[1]))])
    return f"📋 *Gastos fixos do mês:*\n\n{linhas}\n\n*Total: {fbrs(d['fixos'])}*"

def lista_gastos_fixos(user):
    """Lista TODOS os gastos fixos cadastrados + valor do mês de trabalho."""
    mes_inicio = user.get("mes_inicio") or ""
    idx = idx_trabalho(mes_inicio)
    db = get_db(); cur = db.cursor(dictionary=True)
    cur.execute("SELECT id, nome FROM gastos WHERE user_id=%s ORDER BY nome", (user["id"],))
    gastos = cur.fetchall()
    total = 0.0
    linhas = []
    for g in gastos:
        cur.execute("SELECT valor FROM gasto_valores WHERE gasto_id=%s AND idx=%s", (g["id"], idx))
        row = cur.fetchone()
        v = float(row["valor"]) if row else 0.0
        if v != 0:
            total += v
            linhas.append(f"• *{limpar_md(g['nome'])}*: {fbrs(v)}")
        else:
            linhas.append(f"• *{limpar_md(g['nome'])}*: sem valor")
    cur.close(); db.close()
    if not linhas:
        return "📋 Nenhum gasto fixo cadastrado ainda. Cadastre no site primeiro."
    mes_nome = nome_mes_longo(mes_inicio, idx)
    txt = f"📋 *Seus gastos fixos — {mes_nome}*\n\n" + "\n".join(linhas)
    if total:
        txt += f"\n\n*Total: {fbrs(total)}*"
    return txt

def relatorio_lancamentos_mes(user):
    idx = idx_trabalho(user.get("mes_inicio") or "")
    d = calc_totais_mes(user, idx)
    if not d["detalhes_lanc"]:
        return "⚡ Nenhum lançamento registrado neste mês ainda."
    linhas = "\n".join([f"• {desc}: R$ {v:.2f}" for desc, v in d["detalhes_lanc"]])
    return f"⚡ *Lançamentos do mês:*\n\n{linhas}\n\n*Total: R$ {d['lancamentos']:.2f}*"

def relatorio_metas(user):
    db = get_db(); cur = db.cursor(dictionary=True)
    cur.execute("SELECT nome, valor_alvo, valor_atual FROM metas WHERE user_id=%s", (user["id"],))
    metas = cur.fetchall(); cur.close(); db.close()
    if not metas:
        return "🎯 Nenhuma meta cadastrada ainda."
    linhas = []
    for m in metas:
        alvo = float(m["valor_alvo"]); atual = float(m["valor_atual"])
        pct = (atual/alvo*100) if alvo else 0
        linhas.append(f"• *{m['nome']}*: R$ {atual:.2f} / R$ {alvo:.2f} ({pct:.0f}%)")
    return "🎯 *Suas metas:*\n\n" + "\n".join(linhas)

def relatorio_historico(user):
    mes_inicio = user.get("mes_inicio") or ""
    ih = idx_trabalho(mes_inicio)
    if ih <= 0:
        return "📅 Nenhum mês no histórico ainda."
    linhas = []
    for i in range(max(0, ih-6), ih):
        d = calc_totais_mes(user, i)
        if not mes_inicio:
            continue
        y0, m0 = map(int, mes_inicio.split("-"))
        mes_total = (y0*12 + m0 - 1) + i
        ano = mes_total // 12
        mes = mes_total % 12 + 1
        nome_mes = ["jan","fev","mar","abr","mai","jun","jul","ago","set","out","nov","dez"][mes-1]
        emoji = "✅" if d["sobra"] >= 0 else "⚠️"
        linhas.append(f"{emoji} {nome_mes}/{ano}: sobra R$ {d['sobra']:.2f}")
    if not linhas:
        return "📅 Nenhum mês no histórico ainda."
    return "📅 *Histórico (últimos meses):*\n\n" + "\n".join(linhas)

def menu_relatorios():
    return [
        [{"text": "📊 Resumo do mês", "callback_data": "rel_resumo"}],
        [{"text": "📋 Gastos fixos", "callback_data": "rel_fixos"}],
        [{"text": "⚡ Lançamentos do mês", "callback_data": "rel_lancamentos"}],
        [{"text": "🎯 Metas", "callback_data": "rel_metas"}],
        [{"text": "📅 Histórico", "callback_data": "rel_historico"}],
    ]

# ── /RECEBER (A receber: conta → pessoa → mês, tudo por botão) ──
# Fluxo sem digitar nada além do comando inicial:
#   /receber → escolhe a conta (ex: Airbnb) → escolhe a pessoa →
#   escolhe o mês → marca/desmarca como pago.
def rec_mes_label(mes_ref, k):
    """'2026-01' + k(1-based) -> 'jan/26'."""
    try:
        y, m = map(int, mes_ref.split("-"))
        tot = (y * 12 + m - 1) + (k - 1)
        ano, mes = tot // 12, tot % 12 + 1
        nomes = ["jan","fev","mar","abr","mai","jun","jul","ago","set","out","nov","dez"]
        return f"{nomes[mes-1]}/{str(ano)[2:]}"
    except Exception:
        return f"m{k}"

def rec_buscar_contas(user_id):
    db = get_db(); cur = db.cursor(dictionary=True)
    cur.execute("SELECT id, titulo FROM anotacoes WHERE user_id=%s ORDER BY criado_em DESC", (user_id,))
    rows = cur.fetchall(); cur.close(); db.close()
    return rows

def rec_buscar_item(user_id, item_id):
    """Retorna (conta, item, pagas:set, valores:dict) ou (None,...) se não for do usuário."""
    db = get_db(); cur = db.cursor(dictionary=True)
    cur.execute("""SELECT ai.id, ai.anotacao_id, ai.nome, ai.valor_total, ai.qtd, ai.mes_ref, a.titulo
        FROM anotacao_itens ai JOIN anotacoes a ON ai.anotacao_id=a.id
        WHERE ai.id=%s AND a.user_id=%s""", (item_id, user_id))
    item = cur.fetchone()
    if not item:
        cur.close(); db.close()
        return None, None, set(), {}
    cur.execute("SELECT k FROM anotacao_checks WHERE item_id=%s", (item_id,))
    pagas = set(r["k"] for r in cur.fetchall())
    try:
        cur.execute("SELECT k, valor FROM anotacao_valores WHERE item_id=%s", (item_id,))
        valores = {int(r["k"]): round(float(r["valor"] or 0), 2) for r in cur.fetchall()}
    except Exception:
        valores = {}
    cur.close(); db.close()
    return item, item, pagas, valores

def rec_valor_parc(item, valores, k):
    v = valores.get(k)
    if v is not None and v > 0:
        return v
    qtd = int(item["qtd"] or 0)
    if qtd > 0:
        return round(float(item["valor_total"] or 0) / qtd, 2)
    return 0.0

def rec_fbr(v):
    return f"{float(v):.2f}".replace(".", ",")

def teclado_rec_contas(user_id):
    contas = rec_buscar_contas(user_id)
    tecl = []
    for c in contas:
        tecl.append([{"text": f"💰 {c['titulo'][:30]}", "callback_data": f"recv_{c['id']}"}])
    return tecl

def teclado_rec_pessoas(user_id, conta_id):
    db = get_db(); cur = db.cursor(dictionary=True)
    cur.execute("""SELECT ai.id, ai.nome, ai.qtd FROM anotacao_itens ai
        JOIN anotacoes a ON ai.anotacao_id=a.id
        WHERE ai.anotacao_id=%s AND a.user_id=%s ORDER BY ai.id""", (conta_id, user_id))
    itens = cur.fetchall()
    pend_map = {}
    for it in itens:
        cur.execute("SELECT COUNT(*) AS n FROM anotacao_checks WHERE item_id=%s", (it["id"],))
        n = (cur.fetchone() or {}).get("n", 0) or 0
        pend_map[it["id"]] = (int(it["qtd"] or 0) - int(n))
    cur.close(); db.close()
    tecl = []
    for it in itens:
        pend = pend_map.get(it["id"], 0)
        marca = "✅" if pend <= 0 else "⭕"
        tecl.append([{"text": f"{marca} {it['nome'][:28]}", "callback_data": f"recp_{it['id']}"}])
    return tecl

def teclado_rec_meses(item, pagas, valores):
    tecl = []
    linha = []
    for k in range(1, int(item["qtd"] or 0) + 1):
        marca = "✅" if k in pagas else "⭕"
        texto = f"{marca} {rec_mes_label(item['mes_ref'], k)} R${rec_fbr(rec_valor_parc(item, valores, k))}"
        linha.append({"text": texto[:60], "callback_data": f"recm_{item['id']}_{k}"})
        if len(linha) == 2:
            tecl.append(linha); linha = []
    if linha:
        tecl.append(linha)
    return tecl

def rec_texto_meses(item, pagas, valores):
    qtd = int(item["qtd"] or 0)
    pagas_n = len([k for k in range(1, qtd + 1) if k in pagas])
    em_aberto = sum(rec_valor_parc(item, valores, k) for k in range(1, qtd + 1) if k not in pagas)
    return (f"👤 *{limpar_md(item['nome'])}* — {limpar_md(item.get('titulo') or '')}\n"
            f"{pagas_n}/{qtd} pagos · falta R$ {rec_fbr(em_aberto)}\n"
            f"Toque no mês p/ marcar/desmarcar:")

# ── /LANCAR GUIADO (conversa por etapas) ──
CONVERSA_TIMEOUT = 15 * 60  # 15 min sem resposta = expira
conversas = {}  # chat_id -> {"tipo", "etapa", "dados", "inicio"}

def menu_lancar():
    return [
        [{"text": "⚡ Lançamento rápido", "callback_data": "lan_rapido"}],
        [{"text": "💳 Compra parcelada", "callback_data": "lan_parcelado"}],
    ]

def conversa_expirada(chat_id):
    est = conversas.get(chat_id)
    if not est:
        return True
    if time.time() - est.get("inicio", 0) > CONVERSA_TIMEOUT:
        conversas.pop(chat_id, None)
        return True
    return False

def _mes_ou_trabalho(t, mes_inicio):
    """Interpreta o mês digitado; '-' ou vazio = mês de trabalho."""
    t = (t or "").strip()
    if t in ("", "-", "pular"):
        return idx_trabalho(mes_inicio or "")
    return parse_mes(t, mes_inicio or "")

def buscar_gasto_por_nome(user_id, nome):
    db = get_db(); cur = db.cursor(dictionary=True)
    cur.execute("SELECT id, nome, cat FROM gastos WHERE user_id=%s", (user_id,))
    gastos = cur.fetchall()
    cur.close(); db.close()
    norm = normalizar(nome)
    for g in gastos:
        if normalizar(g["nome"]) == norm:
            return g
    return None

def nome_mes_idx(mes_inicio_str, idx):
    try:
        y0, m0 = map(int, mes_inicio_str.split("-"))
        tot = (y0 * 12 + m0 - 1) + idx
        ano, mes = tot // 12, tot % 12 + 1
        nomes = ["jan","fev","mar","abr","mai","jun","jul","ago","set","out","nov","dez"]
        return f"{nomes[mes-1]}/{ano}"
    except Exception:
        return f"mês {idx}"

def nome_mes_longo(mes_inicio_str, idx):
    try:
        y0, m0 = map(int, mes_inicio_str.split("-"))
        tot = (y0 * 12 + m0 - 1) + idx
        ano, mes = tot // 12, tot % 12 + 1
        nomes = ["janeiro","fevereiro","março","abril","maio","junho","julho",
                 "agosto","setembro","outubro","novembro","dezembro"]
        return f"{nomes[mes-1]} de {ano}"
    except Exception:
        return f"mês {idx}"

def limpar_md(t):
    """Tira caracteres que quebrariam o Markdown do Telegram."""
    return re.sub(r"[*_`\[\]]", "", str(t or ""))

def teclado_gastos(user_id):
    try:
        db = get_db(); cur = db.cursor(dictionary=True)
        cur.execute("SELECT id, nome FROM gastos WHERE user_id=%s ORDER BY nome", (user_id,))
        gastos = cur.fetchall(); cur.close(); db.close()
    except Exception:
        gastos = []
    tecl = []
    linha = []
    for g in gastos:
        linha.append({"text": g["nome"][:30], "callback_data": f"relg_{g['id']}"})
        if len(linha) == 2:
            tecl.append(linha); linha = []
    if linha:
        tecl.append(linha)
    return tecl

def teclado_meses(mes_inicio):
    it = idx_trabalho(mes_inicio or "")
    tecl = []
    linha = []
    for i in range(max(0, it - 5), it + 1):
        linha.append({"text": nome_mes_idx(mes_inicio, i), "callback_data": f"relm_{i}"})
        if len(linha) == 3:
            tecl.append(linha); linha = []
    if linha:
        tecl.append(linha)
    return tecl

def gerar_relatorio_whatsapp(user, gasto_id, idx):
    """Relatório por gasto fixo + mês, no formato do site (p/ copiar no WhatsApp)."""
    mes_inicio = user.get("mes_inicio") or ""
    db = get_db(); cur = db.cursor(dictionary=True)
    cur.execute("SELECT id, nome, cat FROM gastos WHERE id=%s AND user_id=%s", (gasto_id, user["id"]))
    g = cur.fetchone()
    if not g:
        cur.close(); db.close()
        return "❌ Gasto não encontrado."
    cur.execute("SELECT valor FROM gasto_valores WHERE gasto_id=%s AND idx=%s", (gasto_id, idx))
    row = cur.fetchone()
    valor_base = round(float(row["valor"]) if row else 0.0, 2)
    cur.execute("SELECT descricao, valor, tipo_ajuste, motivo, local_nome, criado_em, mes_idx FROM lancamentos WHERE user_id=%s", (user["id"],))
    todos = cur.fetchall()
    cur.close(); db.close()

    nome_norm = normalizar(g["nome"])
    lancs = []
    for l in todos:
        if l.get("mes_idx") is not None:
            li = l["mes_idx"]
        else:
            d = l["criado_em"]
            li = idx_do_mes(mes_inicio, d.year, d.month)
        if li != idx:
            continue
        if nome_norm in normalizar(l["descricao"] or ""):
            lancs.append(l)

    total_ajustes = 0.0
    sum_sub = 0.0
    sum_som = 0.0
    for l in lancs:
        sub = l.get("tipo_ajuste") == "subtrair" or (not l.get("tipo_ajuste") and re.match(r"(?i)^desconto\s*:", l["descricao"] or ""))
        som = l.get("tipo_ajuste") == "somar" or (not l.get("tipo_ajuste") and re.match(r"(?i)^acr[eé]scimo\s*:", l["descricao"] or ""))
        if sub:
            total_ajustes -= float(l["valor"])
            sum_sub += float(l["valor"])
        elif som:
            total_ajustes += float(l["valor"])
            sum_som += float(l["valor"])
    # convenção: negativo = a pagar. Base = atual + sinal*(descontos − acréscimos)
    if valor_base == 0:
        valor_original = round(valor_base - total_ajustes, 2)
    else:
        valor_original = round(valor_base + (-1 if valor_base < 0 else 1) * (sum_sub - sum_som), 2)

    mes_nome = nome_mes_longo(mes_inicio, idx)
    fbr = lambda v: f"{v:.2f}".replace(".", ",")
    # texto curto, pronto p/ copiar e colar no WhatsApp
    linhas = []
    linhas.append(f"*{limpar_md(g['nome'])} — {mes_nome}*")
    if lancs:
        linhas.append(f"Base: {fbrs(valor_original)}")
        for l in lancs:
            d = l["criado_em"]
            dia = f"{d.day:02d}/{d.month:02d}"
            sub = l.get("tipo_ajuste") == "subtrair"
            som = l.get("tipo_ajuste") == "somar"
            sinal = "➖" if sub else ("➕" if som else "•")
            linha = f"{sinal} {dia} {limpar_md(l['descricao'])} (R$ {fbr(float(l['valor']))})"
            if l.get("motivo"):
                linha += f" — {limpar_md(l['motivo'])}"
            linhas.append(linha)
        linhas.append(f"*Final: {fbrs(valor_base)}*")
    else:
        linhas.append(f"*Valor: {fbrs(valor_base)}*")
    return "\n".join(linhas)

def calcular_parcelado(user, linhas):
    """Valida a parcelada e monta o pendente SEM salvar.
    Retorna (pendente, erro). kind = 'pfixo' (soma no fixo) | 'pnovo'."""
    nome = linhas[0].strip()
    total = parse_valor(linhas[1]) if len(linhas) > 1 else None
    m = re.match(r"(\d+)", linhas[2]) if len(linhas) > 2 else None
    qtd = int(m.group(1)) if m else 0
    mes_raw = linhas[3] if len(linhas) > 3 else ""
    motivo = linhas[4].strip() if len(linhas) > 4 else ""
    if not nome:
        return None, "❌ Faltou o nome do gasto."
    if total is None or total == 0:
        return None, "❌ Não entendi o valor total. Use algo como `1200`."
    if not 1 <= qtd <= 48:
        return None, "❌ Quantidade inválida. Manda de 1 a 48 (ex: `12` ou `12x`)."
    mes_inicio = user.get("mes_inicio") or ""
    idx_ini = _mes_ou_trabalho(mes_raw, mes_inicio)
    if idx_ini is None:
        return None, "❌ Não entendi o mês. Manda tipo `agosto`, `08/2026` ou deixa em branco."
    # compra parcelada é sempre dívida: guarda negativo (a pagar)
    total = -abs(total)
    vp = round(total / qtd, 2)
    gasto = buscar_gasto_por_nome(user["id"], nome)
    if gasto:
        db = get_db(); cur = db.cursor(dictionary=True)
        linhas_prev = []
        feitas = 0
        for k in range(qtd):
            idx = idx_ini + k
            if idx >= 48:
                break
            cur.execute("SELECT valor FROM gasto_valores WHERE gasto_id=%s AND idx=%s", (gasto["id"], idx))
            row = cur.fetchone()
            antes = round(float(row["valor"]) if row else 0.0, 2)
            linhas_prev.append((idx, antes, round(antes + vp, 2)))
            feitas += 1
        cur.close(); db.close()
        return {"kind": "pfixo", "gasto_id": gasto["id"], "gasto_nome": gasto["nome"],
                "idx_ini": idx_ini, "qtd": qtd, "vp": vp, "total": total,
                "motivo": motivo, "linhas": linhas_prev, "feitas": feitas,
                "ignoradas": qtd - feitas}, None
    return {"kind": "pnovo", "nome": nome, "qtd": qtd, "vp": vp, "total": total,
            "idx_ini": idx_ini, "motivo": motivo}, None

def executar_parcelado(user, p):
    """Salva o pendente da parcelada. Retorna (info, texto)."""
    if p["kind"] == "pfixo":
        db = get_db(); cur = db.cursor(dictionary=True)
        for (idx, antes, depois) in p["linhas"]:
            cur.execute("INSERT INTO gasto_valores (gasto_id,idx,valor) VALUES (%s,%s,%s) ON DUPLICATE KEY UPDATE valor=%s",
                (p["gasto_id"], idx, depois, depois))
        cur.close(); db.close()
        mes_inicio = user.get("mes_inicio") or ""
        resp = (f"✅ *{p['gasto_nome']}* atualizado!\n"
                f"{p['feitas']}x de R$ {abs(p['vp']):.2f} somadas mês a mês\n"
                f"📅 {nome_mes_idx(mes_inicio, p['idx_ini'])} → {nome_mes_idx(mes_inicio, p['idx_ini'] + p['feitas'] - 1)}")
        if p["ignoradas"] > 0:
            resp += f"\n⚠️ {p['ignoradas']} parcela(s) além de 48 meses ignoradas."
        if p["motivo"]:
            resp += f"\n💬 _{p['motivo']}_"
        return None, resp
    db = get_db(); cur = db.cursor()
    cur.execute(
        "INSERT INTO parcelas (user_id,nome,cat,total,qtd,mes_idx,valor_parcela) VALUES (%s,%s,'outros',%s,%s,%s,%s)",
        (user["id"], p["nome"], round(p["total"], 2), p["qtd"], p["idx_ini"], p["vp"]))
    cur.close(); db.close()
    mes_inicio = user.get("mes_inicio") or ""
    resp = (f"✅ Parcelado registrado!\n*{p['nome']}* — {p['qtd']}x de R$ {abs(p['vp']):.2f} "
            f"(total R$ {abs(p['total']):.2f})\n"
            f"📅 {nome_mes_idx(mes_inicio, p['idx_ini'])} → {nome_mes_idx(mes_inicio, p['idx_ini'] + p['qtd'] - 1)}")
    if p["motivo"]:
        resp += f"\n💬 _{p['motivo']}_"
    return None, resp

def processar_parcelado(user, linhas):
    # fora do /lancar guiado: calcula e já salva, como antes
    p, err = calcular_parcelado(user, linhas)
    if err:
        return err
    _, texto = executar_parcelado(user, p)
    return texto

def menu_foto():
    return [[{"text": "📎 Sim, anexar foto", "callback_data": "lan_foto"}],
            [{"text": "Não, continuar sem foto", "callback_data": "lan_nofoto"}]]

def teclado_confirmar(com_foto=True):
    rows = [[{"text": "✅ Confirmar", "callback_data": "lan_ok"}]]
    if com_foto:
        rows.append([{"text": "📎 Anexar foto", "callback_data": "lan_foto"}])
    rows.append([{"text": "❌ Cancelar", "callback_data": "lan_no"}])
    return rows

def resumo_confirmacao(user, est):
    """Texto anterior → novo p/ confirmar antes de salvar."""
    p = est["pendente"]
    nf = len(est.get("fotos", []))
    mes_inicio = user.get("mes_inicio") or ""
    if est["tipo"] == "parcelado":
        if p["kind"] == "pfixo":
            linhas = [f"{nome_mes_idx(mes_inicio, i)}: R$ {fnum(a)} → R$ {fnum(d)}"
                       for (i, a, d) in p["linhas"][:10]]
            if len(p["linhas"]) > 10:
                linhas.append(f"… (+{len(p['linhas']) - 10} meses)")
            txt = (f"💳 *Confirma a parcelada?*\n*{limpar_md(p['gasto_nome'])}* — soma mês a mês:\n"
                   + "\n".join(linhas))
            if p["ignoradas"] > 0:
                txt += f"\n⚠️ {p['ignoradas']} além de 48 meses ignoradas."
        else:
            linhas = []
            for k in range(p["qtd"]):
                i = p["idx_ini"] + k
                linhas.append(f"{nome_mes_idx(mes_inicio, i)}: R$ {fnum(0)} → R$ {fnum(-abs(p['vp']))}")
            if p["qtd"] > 10:
                linhas = linhas[:10] + [f"… (+{p['qtd'] - 10} meses)"]
            txt = (f"💳 *Confirma a parcelada?*\n*{limpar_md(p['nome'])}* — "
                   f"{p['qtd']}x de R$ {fnum(abs(p['vp']))} (total R$ {fnum(abs(p['total']))})\n"
                   f"📅 {nome_mes_idx(mes_inicio, p['idx_ini'])} → "
                   f"{nome_mes_idx(mes_inicio, p['idx_ini'] + p['qtd'] - 1)} · Nova compra separada\n"
                   + "\n".join(linhas))
        if p.get("motivo"):
            txt += f"\n💬 _{limpar_md(p['motivo'])}_"
        return txt
    if p["kind"] == "ajuste":
        txt = (f"⚡ *Confirma o lançamento?*\n*{limpar_md(p['gasto_nome'])}* — "
               f"{nome_mes_longo(mes_inicio, p['idx'])}\n"
               f"Antes: {fbrs(p['anterior'])}\n"
               f"Ajuste: {'−' if p['valor'] < 0 else '+'} R$ {fnum(p['valor_abs'])}\n"
               f"*Depois: {fbrs(p['novo'])}*")
    else:
        d = calc_totais_mes(user, p["idx"])
        antes_lanc = d["lancamentos"]
        depois_lanc = round(antes_lanc + p["valor_abs"], 2)
        txt = (f"⚡ *Confirma o lançamento?*\nNovo avulso: *{limpar_md(p['desc'])}* — "
               f"R$ {fnum(p['valor_abs'])}\n📅 {nome_mes_longo(mes_inicio, p['idx'])}\n"
               f"Lançamentos do mês: R$ {fnum(antes_lanc)} → R$ {fnum(depois_lanc)}")
    if p.get("motivo"):
        txt += f"\n💬 _{limpar_md(p['motivo'])}_"
    if nf:
        txt += f"\n📎 {nf} foto(s) anexada(s)"
    return txt

def limpar_fotos_temp(est):
    for p in (est or {}).get("fotos", []):
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except Exception:
            pass

def baixar_foto(file_id, chat_id):
    """Baixa a foto do Telegram para /tmp. Retorna (caminho, erro)."""
    try:
        r = requests.get(f"{API_URL}/getFile", params={"file_id": file_id}, timeout=20).json()
        if not r.get("ok"):
            return None, "não achei a foto no Telegram."
        fp = r["result"]["file_path"]
        ext = os.path.splitext(fp)[1].lower() or ".jpg"
        url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{fp}"
        dl = requests.get(url, timeout=60)
        if dl.status_code != 200:
            return None, "falha ao baixar a foto."
        os.makedirs("/tmp/fin_fotos", exist_ok=True)
        tmp = f"/tmp/fin_fotos/{chat_id}_{int(time.time())}_{uuid.uuid4().hex[:8]}{ext}"
        with open(tmp, "wb") as fh:
            fh.write(dl.content)
        return tmp, None
    except Exception as e:
        return None, f"erro ao baixar ({e})."

def anexar_fotos(user_id, lid, fotos):
    """Move as fotos p/ uploads e registra em lancamento_anexos. Retorna qtd."""
    ok = 0
    for tmp in fotos or []:
        try:
            if not tmp or not os.path.exists(tmp):
                continue
            ext = os.path.splitext(tmp)[1].lower() or ".jpg"
            nome_final = f"{uuid.uuid4().hex}{ext}"
            os.makedirs(UPLOAD_DIR, exist_ok=True)
            destino = os.path.join(UPLOAD_DIR, nome_final)
            os.rename(tmp, destino)
            tam = os.path.getsize(destino)
            db = get_db(); cur = db.cursor()
            cur.execute("INSERT INTO lancamento_anexos (lancamento_id,nome_original,nome_arquivo,tipo,tamanho) VALUES (%s,%s,%s,%s,%s)",
                (lid, f"telegram_{datetime.now():%Y%m%d_%H%M%S}{ext}", nome_final, "image/jpeg", tam))
            cur.close(); db.close()
            ok += 1
        except Exception as e:
            print(f"[foto] erro ao anexar: {e}")
    return ok

def processar_foto(chat_id, msg):
    user = buscar_usuario_por_chat(chat_id)
    if not user:
        return "⚠️ Vincule sua conta primeiro: `/vincular 123456`"
    est = conversas.get(chat_id)
    if not est or est.get("tipo") != "rapido" or "pendente" not in est \
            or est.get("etapa") not in ("foto?", "aguard_foto", "confirm"):
        if est and est.get("tipo") == "parcelado":
            return "📷 Parcelado não aceita foto. Cancele (/cancelar) e use o ⚡ rápido p/ comprovante."
        return "📷 Não estou esperando foto agora. Use /lancar para começar."
    fotos = msg.get("photo") or []
    if not fotos:
        return "❌ Não achei a imagem. Envie como foto (não como arquivo)."
    tmp, err = baixar_foto(fotos[-1]["file_id"], chat_id)
    if err:
        return f"❌ {err} Tente de novo ou /cancelar."
    est.setdefault("fotos", []).append(tmp)
    est["etapa"] = "confirm"
    return (resumo_confirmacao(user, est), teclado_confirmar())

def resolver_gasto_espelho(user_id, lanc):
    """Acha o gasto de um espelho: gasto_id (novo) ou prefixo 'Desconto:/Acréscimo: Nome' (antigo).
    Retorna (gasto_dict|None, aproximado)."""
    gid = None
    try:
        gid = lanc.get("gasto_id")
    except Exception:
        gid = None
    db = get_db(); cur = db.cursor(dictionary=True)
    if gid:
        cur.execute("SELECT id, nome FROM gastos WHERE id=%s AND user_id=%s", (gid, user_id))
        row = cur.fetchone()
        if row:
            cur.close(); db.close()
            return row, False
    m = re.match(r"^(Desconto|Acrescimo|Acréscimo)\s*:\s*(.+)$", lanc.get("descricao") or "", re.IGNORECASE)
    if m:
        nome = m.group(2).strip().lower()
        cur.execute("SELECT id, nome FROM gastos WHERE user_id=%s", (user_id,))
        for g in cur.fetchall():
            if (g["nome"] or "").strip().lower() == nome:
                cur.close(); db.close()
                return g, True
    cur.close(); db.close()
    return None, False

def reverter_lancamento_db(user_id, lid):
    """Reverte um lançamento no banco. Retorna dict resumo. Levanta ValueError se inválido."""
    db = get_db(); cur = db.cursor(dictionary=True)
    cur.execute("SELECT * FROM lancamentos WHERE id=%s AND user_id=%s", (lid, user_id))
    l = cur.fetchone()
    if not l:
        cur.close(); db.close()
        raise ValueError("Lançamento não encontrado.")
    valor = float(l["valor"] or 0)
    tipo = l.get("tipo_ajuste")
    mes_idx = l.get("mes_idx")
    gasto_nome = None; antes = None; depois = None; aproximado = False; tem_gasto = False
    if tipo in ("subtrair", "somar") and mes_idx is not None:
        g, aproximado = resolver_gasto_espelho(user_id, l)
        if g:
            tem_gasto = True
            gasto_nome = g["nome"]
            cur.execute("SELECT valor FROM gasto_valores WHERE gasto_id=%s AND idx=%s", (g["id"], mes_idx))
            row = cur.fetchone()
            atual = float(row["valor"]) if row else 0.0
            novo = desfazer_ajuste(atual, tipo, valor)
            antes, depois = atual, novo
            if novo == 0:
                cur.execute("DELETE FROM gasto_valores WHERE gasto_id=%s AND idx=%s", (g["id"], mes_idx))
            else:
                cur.execute("INSERT INTO gasto_valores (gasto_id,idx,valor) VALUES (%s,%s,%s) ON DUPLICATE KEY UPDATE valor=%s",
                    (g["id"], mes_idx, novo, novo))
    cur.execute("SELECT nome_arquivo FROM lancamento_anexos WHERE lancamento_id=%s", (lid,))
    n_anexos = 0
    for a in cur.fetchall():
        n_anexos += 1
        try:
            caminho = os.path.join(UPLOAD_DIR, a["nome_arquivo"])
            if os.path.exists(caminho):
                os.remove(caminho)
        except Exception:
            pass
    cur.execute("DELETE FROM lancamentos WHERE id=%s", (lid,))
    cur.close(); db.close()
    return {"desc": l.get("descricao"), "valor": valor, "tipo": tipo, "mes_idx": mes_idx,
            "gasto_nome": gasto_nome, "antes": antes, "depois": depois,
            "aproximado": aproximado, "tem_gasto": tem_gasto, "n_anexos": n_anexos}

def texto_reverter_preview(user, l):
    mes_inicio = user.get("mes_inicio") or ""
    if not l.get("tipo_ajuste"):
        return (f"↩️ *Reverter?*\n\"{limpar_md(l.get('descricao'))}\" — R$ {float(l['valor'] or 0):.2f}\n"
                f"Apaga o lançamento.")
    g, _ = resolver_gasto_espelho(user["id"], l)
    op = "desconto" if l.get("tipo_ajuste") == "subtrair" else "acréscimo"
    if g and l.get("mes_idx") is not None:
        db = get_db(); cur = db.cursor(dictionary=True)
        cur.execute("SELECT valor FROM gasto_valores WHERE gasto_id=%s AND idx=%s", (g["id"], l["mes_idx"]))
        row = cur.fetchone(); cur.close(); db.close()
        atual = float(row["valor"]) if row else 0.0
        novo = desfazer_ajuste(atual, l.get("tipo_ajuste"), l.get("valor") or 0)
        return (f"↩️ *Reverter {op}?*\n*{limpar_md(g['nome'])}* — {nome_mes_longo(mes_inicio, l['mes_idx'])}\n"
                f"{fbrs(atual)} → {fbrs(novo)}\nApaga o registro espelho.")
    return (f"↩️ *Reverter {op}?*\n\"{limpar_md(l.get('descricao'))}\"\n"
            f"Registro antigo sem vínculo: apaga só o espelho, o fixo NÃO muda.")

def processar_conversa(chat_id, user, texto):
    """Após escolher o tipo no /lancar, o usuário manda TUDO em uma mensagem."""
    if conversa_expirada(chat_id):
        return "⏰ A conversa expirou. Mande /lancar para começar de novo."
    est = conversas[chat_id]
    # etapas de foto/confirmação: orienta a usar os botões (aceita sim/não por texto)
    if est.get("etapa") in ("foto?", "aguard_foto", "confirm"):
        t = texto.strip().lower()
        if est.get("tipo") == "rapido" and "pendente" in est:
            if t in ("sim", "s", "anexar", "foto", "com foto"):
                est["etapa"] = "aguard_foto"
                return "📸 Envie a foto do comprovante agora (ou /cancelar para desistir)."
            if t in ("não", "nao", "n", "sem foto", "nao quero", "não quero", "continuar"):
                est["etapa"] = "confirm"
                return (resumo_confirmacao(user, est), teclado_confirmar())
        if est.get("etapa") == "aguard_foto":
            return "📸 Envie a foto agora (ou /cancelar para desistir)."
        return "👆 Toque nos botões acima para continuar (ou /cancelar)."
    linhas = [l.strip() for l in texto.strip().split("\n") if l.strip()]
    if est["tipo"] == "rapido":
        if len(linhas) < 2:
            return ("⚠️ Formato incompleto. Envie tudo em uma mensagem assim:\n"
                    "```\nDescrição\nValor\nMês (opcional)\nMotivo (opcional)\n```")
        p, err = calcular_lancamento(user, linhas)
        if err:
            return err  # mantém a conversa p/ corrigir e reenviar
        est["pendente"] = p
        est["etapa"] = "foto?"
        return ("📎 Quer anexar foto do comprovante?", menu_foto())
    if len(linhas) < 3:
        return ("⚠️ Formato incompleto. Envie tudo em uma mensagem assim:\n"
                "```\nNome do gasto\nValor total\nQuantas parcelas\nMês início (opcional)\nMotivo (opcional)\n```")
    p, err = calcular_parcelado(user, linhas)
    if err:
        return err  # mantém a conversa p/ corrigir e reenviar
    est["pendente"] = p
    est["etapa"] = "confirm"
    return (resumo_confirmacao(user, est), teclado_confirmar(com_foto=False))

def processar_callback(chat_id, callback_data):
    user = buscar_usuario_por_chat(chat_id)
    if not user:
        return "⚠️ Você não está vinculado a nenhuma conta."
    # ── /receber: conta → pessoa → mês ──
    if callback_data.startswith("recv_"):
        try:
            nid = int(callback_data.split("_", 1)[1])
        except Exception:
            return "❌ Opção inválida."
        db = get_db(); cur = db.cursor(dictionary=True)
        cur.execute("SELECT id, titulo FROM anotacoes WHERE id=%s AND user_id=%s", (nid, user["id"]))
        conta = cur.fetchone(); cur.close(); db.close()
        if not conta:
            return "❌ Conta não encontrada."
        tecl = teclado_rec_pessoas(user["id"], nid)
        if not tecl:
            return f"💰 *{limpar_md(conta['titulo'])}*\nSem pessoas ainda. Adicione no site primeiro."
        return (f"💰 *{limpar_md(conta['titulo'])}*\nQuem?", tecl)
    if callback_data.startswith("recp_"):
        try:
            iid = int(callback_data.split("_", 1)[1])
        except Exception:
            return "❌ Opção inválida."
        _, item, pagas, valores = rec_buscar_item(user["id"], iid)
        if not item:
            return "❌ Pessoa não encontrada."
        return (rec_texto_meses(item, pagas, valores), teclado_rec_meses(item, pagas, valores))
    if callback_data.startswith("recm_"):
        try:
            _, iid_s, k_s = callback_data.split("_")
            iid, k = int(iid_s), int(k_s)
        except Exception:
            return "❌ Opção inválida."
        _, item, pagas, valores = rec_buscar_item(user["id"], iid)
        if not item:
            return "❌ Pessoa não encontrada."
        if not 1 <= k <= int(item["qtd"] or 0):
            return "❌ Mês inválido."
        db = get_db(); cur = db.cursor()
        cur.execute("SELECT k FROM anotacao_checks WHERE item_id=%s AND k=%s", (iid, k))
        if cur.fetchone():
            cur.execute("DELETE FROM anotacao_checks WHERE item_id=%s AND k=%s", (iid, k))
            pago = False
        else:
            cur.execute("INSERT INTO anotacao_checks (item_id,k) VALUES (%s,%s)", (iid, k))
            pago = True
        cur.close(); db.close()
        _, item, pagas, valores = rec_buscar_item(user["id"], iid)
        v = rec_valor_parc(item, valores, k)
        status = "Pago ✅" if pago else "Desmarcado ⭕"
        texto = (f"{'✅' if pago else '⭕'} *{limpar_md(item['nome'])}* — {rec_mes_label(item['mes_ref'], k)} "
                 f"(R$ {rec_fbr(v)}): *{status}*\n\n" + rec_texto_meses(item, pagas, valores))
        return (texto, teclado_rec_meses(item, pagas, valores))
    if callback_data.startswith("relg_"):
        try:
            gid = int(callback_data.split("_", 1)[1])
        except Exception:
            return "❌ Opção inválida."
        db = get_db(); cur = db.cursor(dictionary=True)
        cur.execute("SELECT id, nome FROM gastos WHERE id=%s AND user_id=%s", (gid, user["id"]))
        row = cur.fetchone(); cur.close(); db.close()
        if not row:
            return "❌ Gasto não encontrado."
        conversas[chat_id] = {"tipo": "relatorio", "gasto": gid, "inicio": time.time()}
        return (f"*{row['nome']}*\nQual mês?", teclado_meses(user.get("mes_inicio") or ""))
    if callback_data.startswith("relm_"):
        try:
            idx = int(callback_data.split("_", 1)[1])
        except Exception:
            return "❌ Opção inválida."
        est = conversas.get(chat_id)
        if not est or est.get("tipo") != "relatorio" or "gasto" not in est:
            return "⏰ Sessão expirada. Mande /relatorio de novo."
        if conversa_expirada(chat_id):
            return "⏰ A conversa expirou. Mande /relatorio de novo."
        gid = est["gasto"]
        conversas.pop(chat_id, None)
        try:
            return gerar_relatorio_whatsapp(user, gid, idx)
        except Exception as e:
            return f"❌ Erro ao gerar relatório: {e}"
    if callback_data == "lan_rapido" or callback_data == "lan_parcelado":
        tipo = "rapido" if callback_data == "lan_rapido" else "parcelado"
        conversas[chat_id] = {"tipo": tipo, "inicio": time.time()}
        if tipo == "rapido":
            return ("⚡ *Lançamento rápido*\n"
                    "Envie tudo em uma mensagem assim:\n"
                    "```\nGasto fixo ou novo\nValor\nMês (opcional)\nMotivo (opcional)\n```\n"
                    "• Se for um gasto fixo já cadastrado → ajusta ele (use `-` p/ subtrair)\n"
                    "• Se for novo → cria avulso\n"
                    "• No fim eu mostro antes → depois p/ confirmar e pergunto da foto 📎\n\n"
                    "*Exemplo:*\n```\nNubank\n12,50\noutubro\nLanche da tarde\n```")
        return ("💳 *Compra parcelada*\n"
                "Envie assim:\n"
                "```\nNome do gasto\nValor total\nQuantas parcelas\nMês início (opcional)\nMotivo (opcional)\n```\n"
                "• Se o Nome bater com um gasto fixo já cadastrado → soma as parcelas nele, mês a mês\n"
                "• Se não bater → cria uma compra parcelada nova e separada\n"
                "• No fim eu mostro tudo p/ confirmar antes de salvar\n\n"
                "*Exemplo:*\n```\nCartão Nubank\n1200\n12x\nagosto\nTV nova\n```")
    # ── confirmação do /lancar ──
    if callback_data in ("lan_foto", "lan_nofoto"):
        est = conversas.get(chat_id)
        if not est or est.get("tipo") != "rapido" or "pendente" not in est \
                or conversa_expirada(chat_id):
            return "⏰ Sessão expirada. Mande /lancar de novo."
        if callback_data == "lan_foto":
            est["etapa"] = "aguard_foto"
            return "📸 Envie a foto do comprovante agora (ou /cancelar para desistir)."
        est["etapa"] = "confirm"
        return (resumo_confirmacao(user, est), teclado_confirmar())
    if callback_data == "lan_ok":
        est = conversas.get(chat_id)
        if not est or "pendente" not in est or est.get("etapa") != "confirm" \
                or est.get("tipo") not in ("rapido", "parcelado") or conversa_expirada(chat_id):
            return "⏰ Sessão expirada. Mande /lancar de novo."
        try:
            if est["tipo"] == "rapido":
                lid, texto = executar_lancamento(user, est["pendente"])
                nf = anexar_fotos(user["id"], lid, est.get("fotos"))
                if nf:
                    texto += f"\n📎 {nf} foto(s) anexada(s)!"
            else:
                _, texto = executar_parcelado(user, est["pendente"])
        except Exception as e:
            return f"❌ Erro ao salvar: {e}"
        conversas.pop(chat_id, None)
        return texto
    if callback_data == "lan_no":
        est = conversas.pop(chat_id, None)
        limpar_fotos_temp(est)
        return "🚫 Lançamento cancelado. Nada foi salvo."
    # ── /reverter ──
    if callback_data.startswith("revok_"):
        try:
            lid = int(callback_data.split("_", 1)[1])
        except Exception:
            return "❌ Opção inválida."
        est = conversas.get(chat_id)
        if not est or est.get("tipo") != "reverter" or est.get("alvo") != lid \
                or conversa_expirada(chat_id):
            return "⏰ Sessão expirada. Mande /reverter de novo."
        try:
            r = reverter_lancamento_db(user["id"], lid)
        except ValueError as e:
            conversas.pop(chat_id, None)
            return f"❌ {e}"
        except Exception as e:
            return f"❌ Erro ao reverter: {e}"
        conversas.pop(chat_id, None)
        if r["tipo"] in ("subtrair", "somar") and r["tem_gasto"]:
            txt = (f"↩️ Revertido!\n*{limpar_md(r['gasto_nome'])}*: "
                   f"{fbrs(r['antes'])} → {fbrs(r['depois'])}\nRegistro espelho apagado.")
        elif r["tipo"] in ("subtrair", "somar"):
            txt = "↩️ Espelho apagado. (Sem vínculo: o fixo NÃO mudou.)"
        else:
            txt = f"↩️ \"{limpar_md(r['desc'])}\" apagado."
            if r["n_anexos"]:
                txt += f" ({r['n_anexos']} anexo(s) removidos)"
        return txt
    if callback_data == "revno":
        conversas.pop(chat_id, None)
        return "Mantido. Nada foi alterado. 👍"
    if callback_data.startswith("rev_"):
        try:
            lid = int(callback_data.split("_", 1)[1])
        except Exception:
            return "❌ Opção inválida."
        db = get_db(); cur = db.cursor(dictionary=True)
        cur.execute("SELECT * FROM lancamentos WHERE id=%s AND user_id=%s", (lid, user["id"]))
        l = cur.fetchone(); cur.close(); db.close()
        if not l:
            return "❌ Lançamento não encontrado."
        conversas[chat_id] = {"tipo": "reverter", "alvo": lid, "inicio": time.time()}
        return (texto_reverter_preview(user, l),
                [[{"text": "✅ Sim, reverter", "callback_data": f"revok_{lid}"}],
                 [{"text": "❌ Manter", "callback_data": "revno"}]])
    mapa = {
        "rel_resumo": relatorio_resumo_mes,
        "rel_fixos": relatorio_gastos_fixos,
        "rel_lancamentos": relatorio_lancamentos_mes,
        "rel_metas": relatorio_metas,
        "rel_historico": relatorio_historico,
    }
    fn = mapa.get(callback_data)
    if not fn:
        return "❌ Opção inválida."
    try:
        return fn(user)
    except Exception as e:
        return f"❌ Erro ao gerar relatório: {e}"

def processar_mensagem(chat_id, texto):
    texto = texto.strip()

    if texto.startswith("/cancelar"):
        est = conversas.pop(chat_id, None)
        limpar_fotos_temp(est)
        if est:
            return "🚫 Conversa cancelada. Nada foi salvo. Use /lancar para começar de novo."
        return "Nada em andamento. Use /lancar para registrar."

    if texto.startswith("/") and chat_id in conversas:
        # outro comando no meio da conversa: abandona e segue o comando
        limpar_fotos_temp(conversas.get(chat_id))
        conversas.pop(chat_id, None)

    if texto.startswith("/start"):
        return ("👋 Olá! Eu sou o bot de lançamentos do seu app de Finanças.\n\n"
                "Para me vincular à sua conta, vá em *Configurações* no site, "
                "gere um código e me envie:\n`/vincular 123456`\n\n"
                "Depois use /lancar para registrar guiado, ou mande direto no formato:\n"
                "```\nNome do gasto\nValor\nMês (opcional)\nMotivo (opcional)\n```\n\n"
                "Digite /comandos para ver tudo que eu faço!")

    if texto.startswith("/ajuda"):
        return ("📋 *Formato da mensagem:*\n```\nNome do gasto\nValor\nMês (opcional)\nMotivo (opcional)\n```\n\n"
                "• Convenção: valor *negativo = a pagar*, *positivo = a receber*\n"
                "• Se o *Nome* bater com um gasto fixo já cadastrado → ajusta esse gasto (use sinal: -35.42 ou +50)\n"
                "• Se não bater → cria um lançamento avulso novo\n"
                "• *Mês* pode ser tipo `agosto` ou `08/2026`. Se não informar, usa o mês atual.\n"
                "• *Motivo* fica salvo e aparece quando você passar o mouse no valor, no site.\n\n"
                "*Exemplo:*\n```\nMelody\n-35.42\njulho\nFulano pagou a parte dele\n```\n\n"
                "Para parcelado, use /lancar → 💳 (nome bate com fixo = soma nele, senão cria separado).\n\n"
                "Digite /relatorios para ver relatórios rápidos!\n\n"
                "Ou use /lancar para o registro guiado (rápido ou parcelado).")

    if texto.startswith("/comandos"):
        return texto_comandos()

    if texto == "/fixos" or texto.startswith("/fixos "):
        user = buscar_usuario_por_chat(chat_id)
        if not user:
            return ("⚠️ Você ainda não está vinculado a nenhuma conta.\n"
                    "Vá em Configurações no site, gere um código, e me envie:\n`/vincular 123456`")
        return lista_gastos_fixos(user)

    if texto == "/receber" or texto.startswith("/receber ") or texto == "/cobrar" or texto.startswith("/cobrar "):
        user = buscar_usuario_por_chat(chat_id)
        if not user:
            return ("⚠️ Você ainda não está vinculado a nenhuma conta.\n"
                    "Vá em Configurações no site, gere um código, e me envie:\n`/vincular 123456`")
        tecl = teclado_rec_contas(user["id"])
        if not tecl:
            return "💰 Nenhuma conta a receber ainda. Crie uma no site (ex: Airbnb)."
        return ("💰 *Qual conta?*", tecl)

    if texto == "/relatorio" or texto.startswith("/relatorio "):
        user = buscar_usuario_por_chat(chat_id)
        if not user:
            return ("⚠️ Você ainda não está vinculado a nenhuma conta.\n"
                    "Vá em Configurações no site, gere um código, e me envie:\n`/vincular 123456`")
        tecl = teclado_gastos(user["id"])
        if not tecl:
            return "📋 Nenhum gasto fixo cadastrado ainda. Cadastre no site primeiro."
        return ("📱 *Relatório estilo WhatsApp*\nQual gasto fixo?", tecl)

    if texto.startswith("/lancar"):
        user = buscar_usuario_por_chat(chat_id)
        if not user:
            return ("⚠️ Você ainda não está vinculado a nenhuma conta.\n"
                    "Vá em Configurações no site, gere um código, e me envie:\n`/vincular 123456`")
        return "escolher_lancar"  # sinal especial tratado no loop principal

    if texto.startswith("/reverter"):
        user = buscar_usuario_por_chat(chat_id)
        if not user:
            return ("⚠️ Você ainda não está vinculado a nenhuma conta.\n"
                    "Vá em Configurações no site, gere um código, e me envie:\n`/vincular 123456`")
        db = get_db(); cur = db.cursor(dictionary=True)
        cur.execute("SELECT id, descricao, valor, tipo_ajuste, criado_em FROM lancamentos "
                    "WHERE user_id=%s ORDER BY criado_em DESC LIMIT 10", (user["id"],))
        recs = cur.fetchall(); cur.close(); db.close()
        if not recs:
            return "↩️ Nada para reverter ainda."
        tecl = []
        linha = []
        for r in recs:
            ic = "➖" if r.get("tipo_ajuste") == "subtrair" else ("➕" if r.get("tipo_ajuste") == "somar" else "•")
            linha.append({"text": f"{ic} {(r['descricao'] or '')[:26]} R$ {float(r['valor'] or 0):.2f}",
                          "callback_data": f"rev_{r['id']}"})
            if len(linha) == 2:
                tecl.append(linha); linha = []
        if linha:
            tecl.append(linha)
        tecl.append([{"text": "❌ Cancelar", "callback_data": "revno"}])
        conversas[chat_id] = {"tipo": "reverter", "inicio": time.time()}
        return ("↩️ *Qual lançamento reverter?*\n_Reverte tudo: desfaz do fixo e apaga o registro._", tecl)

    # conversa do /lancar em andamento?
    if chat_id in conversas:
        user = buscar_usuario_por_chat(chat_id)
        if not user:
            conversas.pop(chat_id, None)
            return ("⚠️ Você ainda não está vinculado a nenhuma conta.\n"
                    "Vá em Configurações no site, gere um código, e me envie:\n`/vincular 123456`")
        try:
            return processar_conversa(chat_id, user, texto)
        except Exception as e:
            return f"❌ Erro ao processar: {e}"

    if texto.startswith("/vincular"):
        partes = texto.split()
        if len(partes) < 2:
            return "❌ Use assim: `/vincular 123456`"
        codigo = partes[1].strip()
        username = vincular_codigo(chat_id, codigo)
        if username:
            return f"✅ Vinculado com sucesso à conta *{username}*! Agora pode mandar seus lançamentos."
        return "❌ Código inválido ou expirado. Gere um novo no site."

    if texto.startswith("/desvincular"):
        desvincular(chat_id)
        return "🔓 Desvinculado. Use /vincular para conectar novamente."

    if texto.startswith("/relatorios") or texto.startswith("/relatorio"):
        user = buscar_usuario_por_chat(chat_id)
        if not user:
            return ("⚠️ Você ainda não está vinculado a nenhuma conta.\n"
                    "Vá em Configurações no site, gere um código, e me envie:\n`/vincular 123456`")
        return "escolher_relatorio"  # sinal especial tratado no loop principal

    # mensagem normal = tentativa de lançamento
    user = buscar_usuario_por_chat(chat_id)
    if not user:
        return ("⚠️ Você ainda não está vinculado a nenhuma conta.\n"
                "Vá em Configurações no site, gere um código, e me envie:\n`/vincular 123456`")

    linhas = [l for l in texto.split("\n") if l.strip()]
    if len(linhas) < 2:
        return ("⚠️ Formato incompleto. Envie assim:\n```\nNome do gasto\nValor\nMês (opcional)\nMotivo (opcional)\n```\n"
                "Digite /ajuda para mais detalhes.")

    try:
        return processar_lancamento(user, linhas)
    except Exception as e:
        return f"❌ Erro ao processar: {e}"

def texto_comandos():
    return ("⌨️ *Comandos:*\n"
            "/lancar — registro guiado (rápido ou parcelado)\n"
            "/reverter — cancela um lançamento (desfaz do fixo)\n"
            "/receber — marcar cobrança como paga (só botões)\n"
            "/relatorio — relatório estilo WhatsApp (pergunta gasto + mês)\n"
            "/relatorios — relatórios rápidos do mês\n"
            "/fixos — lista seus gastos fixos (nome + valor do mês)\n"
            "/ajuda — formato da mensagem direta\n"
            "/cancelar — cancela a conversa atual\n"
            "/vincular 123456 — vincula tua conta\n"
            "/desvincular — desvincula")

def garantir_colunas_lembrete():
    try:
        db = get_db(); cur = db.cursor()
        cur.execute("ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS telegram_last_seen DATETIME DEFAULT NULL")
        cur.execute("ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS telegram_last_reminder DATETIME DEFAULT NULL")
        cur.close(); db.close()
    except Exception as e:
        print(f"[schema] aviso ao garantir colunas do lembrete: {e}")

def marcar_atividade(chat_id):
    try:
        db = get_db(); cur = db.cursor()
        cur.execute("UPDATE usuarios SET telegram_last_seen=NOW() WHERE telegram_chat_id=%s", (str(chat_id),))
        cur.close(); db.close()
    except Exception as e:
        print(f"[atividade] erro: {e}")

def enviar_lembretes():
    try:
        db = get_db(); cur = db.cursor(dictionary=True)
        cur.execute(
            "SELECT id, telegram_chat_id FROM usuarios "
            "WHERE telegram_chat_id IS NOT NULL AND telegram_last_seen IS NOT NULL "
            f"AND telegram_last_seen <= NOW() - INTERVAL {int(LEMBRETE_HORAS_INATIVO)} HOUR "
            "AND (telegram_last_reminder IS NULL OR telegram_last_reminder < telegram_last_seen)")
        for u in cur.fetchall():
            enviar_msg(u["telegram_chat_id"],
                       "🤖 Ei! Faz um tempo que você não manda nada. Aqui vão os comandos:\n\n"
                       + texto_comandos())
            cur.execute("UPDATE usuarios SET telegram_last_reminder=NOW() WHERE id=%s", (u["id"],))
        cur.close(); db.close()
    except Exception as e:
        print(f"[lembrete] erro: {e}")

_ultimo_lembrete_check = 0.0

def talvez_enviar_lembretes():
    global _ultimo_lembrete_check
    agora = time.time()
    if agora - _ultimo_lembrete_check < 60:
        return
    _ultimo_lembrete_check = agora
    enviar_lembretes()

def main():
    if not BOT_TOKEN:
        print("[ERRO] TELEGRAM_BOT_TOKEN não configurado no .env")
        return
    garantir_colunas_lembrete()
    print("[bot] iniciado, aguardando mensagens...")
    offset = None
    while True:
        try:
            params = {"timeout": 30}
            if offset:
                params["offset"] = offset
            r = requests.get(f"{API_URL}/getUpdates", params=params, timeout=35)
            data = r.json()
            for update in data.get("result", []):
                offset = update["update_id"] + 1

                # botão clicado (callback query)
                cq = update.get("callback_query")
                if cq:
                    chat_id = cq["message"]["chat"]["id"]
                    marcar_atividade(chat_id)
                    callback_data = cq.get("data", "")
                    print(f"[callback] chat={chat_id}: {callback_data}")
                    responder_callback(cq["id"])
                    resposta = processar_callback(chat_id, callback_data)
                    if isinstance(resposta, tuple):
                        enviar_msg(chat_id, resposta[0], teclado=resposta[1])
                    else:
                        enviar_msg(chat_id, resposta)
                    continue

                msg = update.get("message")
                if not msg:
                    continue
                # foto (comprovante do /lancar rápido)
                if "photo" in msg and "text" not in msg:
                    chat_id = msg["chat"]["id"]
                    marcar_atividade(chat_id)
                    print(f"[foto] chat={chat_id}")
                    resposta = processar_foto(chat_id, msg)
                    if isinstance(resposta, tuple):
                        enviar_msg(chat_id, resposta[0], teclado=resposta[1])
                    else:
                        enviar_msg(chat_id, resposta)
                    continue
                if "text" not in msg:
                    continue
                chat_id = msg["chat"]["id"]
                marcar_atividade(chat_id)
                texto = msg["text"]
                print(f"[recebido] chat={chat_id}: {texto[:50]}")
                resposta = processar_mensagem(chat_id, texto)
                if isinstance(resposta, tuple):
                    enviar_msg(chat_id, resposta[0], teclado=resposta[1])
                elif resposta == "escolher_relatorio":
                    enviar_msg(chat_id, "📈 *Escolha o relatório:*", teclado=menu_relatorios())
                elif resposta == "escolher_lancar":
                    enviar_msg(chat_id, "🧾 *O que vai ser?*", teclado=menu_lancar())
                else:
                    enviar_msg(chat_id, resposta)
            talvez_enviar_lembretes()
        except Exception as e:
            print(f"[erro no loop] {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
