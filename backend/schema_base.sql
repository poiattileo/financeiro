CREATE TABLE IF NOT EXISTS usuarios (
    id                INT AUTO_INCREMENT PRIMARY KEY,
    username          VARCHAR(50) UNIQUE NOT NULL,
    senha_hash        VARCHAR(255) NOT NULL,
    salario           DECIMAL(12,2) DEFAULT 0,
    mes_inicio        VARCHAR(7) DEFAULT '',
    role              VARCHAR(20) DEFAULT 'user',
    telegram_chat_id  VARCHAR(50) DEFAULT NULL,
    telegram_link_code VARCHAR(10) DEFAULT NULL,
    telegram_last_seen     DATETIME DEFAULT NULL,
    telegram_last_reminder DATETIME DEFAULT NULL,
    criado_em         DATETIME DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS gastos (
    id        INT AUTO_INCREMENT PRIMARY KEY,
    user_id   INT NOT NULL,
    nome      VARCHAR(100) NOT NULL,
    cat       VARCHAR(50) NOT NULL,
    criado_em DATETIME DEFAULT NOW(),
    FOREIGN KEY (user_id) REFERENCES usuarios(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS gasto_valores (
    gasto_id INT NOT NULL,
    idx      INT NOT NULL,
    valor    DECIMAL(12,2) NOT NULL,
    PRIMARY KEY (gasto_id, idx),
    FOREIGN KEY (gasto_id) REFERENCES gastos(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS parcelas (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    user_id       INT NOT NULL,
    nome          VARCHAR(100) NOT NULL,
    cat           VARCHAR(50) NOT NULL,
    total         DECIMAL(12,2) NOT NULL,
    qtd           TINYINT NOT NULL,
    mes_idx       INT NOT NULL,
    valor_parcela DECIMAL(12,2) NOT NULL,
    criado_em     DATETIME DEFAULT NOW(),
    FOREIGN KEY (user_id) REFERENCES usuarios(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS lancamentos (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    user_id     INT NOT NULL,
    descricao   VARCHAR(150) NOT NULL,
    valor       DECIMAL(12,2) NOT NULL,
    cat         VARCHAR(50) NOT NULL DEFAULT 'outros',
    local_nome  VARCHAR(100) DEFAULT '',
    recorrencia VARCHAR(20) DEFAULT 'nunca',
    motivo      VARCHAR(200) DEFAULT '',
    mes_idx     INT DEFAULT NULL,
    tipo_ajuste VARCHAR(20) DEFAULT NULL,
    gasto_id    INT DEFAULT NULL,
    criado_em   DATETIME DEFAULT NOW(),
    FOREIGN KEY (user_id) REFERENCES usuarios(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS metas (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    user_id     INT NOT NULL,
    nome        VARCHAR(100) NOT NULL,
    valor_alvo  DECIMAL(12,2) NOT NULL,
    valor_atual DECIMAL(12,2) DEFAULT 0,
    cor         VARCHAR(20) DEFAULT 'green',
    criado_em   DATETIME DEFAULT NOW(),
    FOREIGN KEY (user_id) REFERENCES usuarios(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS categorias (
    id       INT AUTO_INCREMENT PRIMARY KEY,
    nome     VARCHAR(50) UNIQUE NOT NULL,
    label    VARCHAR(80) NOT NULL,
    emoji    VARCHAR(10) DEFAULT '💳',
    cor      VARCHAR(20) DEFAULT '#94a3b8',
    tipo     VARCHAR(20) DEFAULT 'ambos'
);

CREATE TABLE IF NOT EXISTS lancamento_anexos (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    lancamento_id   INT NOT NULL,
    nome_original   VARCHAR(255) NOT NULL,
    nome_arquivo    VARCHAR(255) NOT NULL,
    tipo            VARCHAR(100) DEFAULT 'application/octet-stream',
    tamanho         INT DEFAULT 0,
    criado_em       DATETIME DEFAULT NOW(),
    FOREIGN KEY (lancamento_id) REFERENCES lancamentos(id) ON DELETE CASCADE
);

-- Snapshots congelados: mês fechado sai da tela principal e vive só no histórico.
-- mes_ref é absoluto (YYYY-MM) para sobreviver a trocas de mes_inicio.
CREATE TABLE IF NOT EXISTS historico_meses (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    user_id     INT NOT NULL,
    mes_ref     VARCHAR(7) NOT NULL,
    mes_idx     INT NOT NULL DEFAULT 0,
    salario     DECIMAL(12,2) DEFAULT 0,
    fixos       DECIMAL(12,2) DEFAULT 0,
    parcelas    DECIMAL(12,2) DEFAULT 0,
    lancamentos DECIMAL(12,2) DEFAULT 0,
    total       DECIMAL(12,2) DEFAULT 0,
    sobra       DECIMAL(12,2) DEFAULT 0,
    detalhes    TEXT NULL,
    criado_em   DATETIME DEFAULT NOW(),
    UNIQUE KEY uq_hist_user_mes (user_id, mes_ref),
    FOREIGN KEY (user_id) REFERENCES usuarios(id) ON DELETE CASCADE
);

-- Anotações (controle paralelo: não entra em nenhum total)
CREATE TABLE IF NOT EXISTS anotacoes (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    titulo VARCHAR(100) NOT NULL,
    criado_em DATETIME DEFAULT NOW(),
    FOREIGN KEY (user_id) REFERENCES usuarios(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS anotacao_itens (
    id INT AUTO_INCREMENT PRIMARY KEY,
    anotacao_id INT NOT NULL,
    nome VARCHAR(100) NOT NULL,
    valor_total DECIMAL(12,2) NOT NULL,
    qtd INT NOT NULL,
    mes_ref VARCHAR(7) NOT NULL,
    FOREIGN KEY (anotacao_id) REFERENCES anotacoes(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS anotacao_checks (
    item_id INT NOT NULL,
    k INT NOT NULL,
    marcado_em DATETIME DEFAULT NOW(),
    PRIMARY KEY (item_id, k),
    FOREIGN KEY (item_id) REFERENCES anotacao_itens(id) ON DELETE CASCADE
);

-- Valores customizados por mês (ex: Airbnb varia). Se não houver override,
-- vale valor_total/qtd. Nunca entra em totais: só lembrete de cobrança.
CREATE TABLE IF NOT EXISTS anotacao_valores (
    item_id INT NOT NULL,
    k INT NOT NULL,
    valor DECIMAL(12,2) NOT NULL,
    PRIMARY KEY (item_id, k),
    FOREIGN KEY (item_id) REFERENCES anotacao_itens(id) ON DELETE CASCADE
);


INSERT IGNORE INTO categorias (nome,label,emoji,cor,tipo) VALUES
('moradia',     'Moradia',      '🏠','#60a5fa','gasto'),
('transporte',  'Transporte',   '🚗','#fbbf24','ambos'),
('alimentacao', 'Alimentação',  '🍔','#f97316','ambos'),
('saude',       'Saúde',        '💊','#34d399','ambos'),
('educacao',    'Educação',     '📚','#f472b6','gasto'),
('assinaturas', 'Assinaturas',  '📱','#a78bfa','gasto'),
('financiamento','Financiamento','🏦','#f87171','gasto'),
('eletronicos', 'Eletrônicos',  '💻','#818cf8','gasto'),
('moveis',      'Móveis',       '🛋️','#fb923c','gasto'),
('roupas',      'Roupas',       '👕','#e879f9','ambos'),
('lazer',       'Lazer',        '🎮','#c084fc','lancamento'),
('outros',      'Outros',       '💳','#94a3b8','ambos');
