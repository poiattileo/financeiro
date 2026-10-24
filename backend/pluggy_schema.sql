CREATE TABLE IF NOT EXISTS pluggy_items (
  id VARCHAR(64) PRIMARY KEY,
  user_id INT NOT NULL,
  connector_name VARCHAR(160) DEFAULT '',
  source VARCHAR(20) NOT NULL DEFAULT 'widget',
  status VARCHAR(40) DEFAULT 'CONNECTING',
  last_sync DATETIME NULL,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_pi_user (user_id),
  FOREIGN KEY (user_id) REFERENCES usuarios(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS pluggy_accounts (
  id VARCHAR(64) PRIMARY KEY,
  item_id VARCHAR(64) NOT NULL,
  user_id INT NOT NULL,
  name VARCHAR(160) NOT NULL,
  type VARCHAR(20) NOT NULL,
  balance DECIMAL(15,2) NULL,
  currency_code VARCHAR(8) DEFAULT 'BRL',
  INDEX idx_pa_user (user_id),
  FOREIGN KEY (item_id) REFERENCES pluggy_items(id) ON DELETE CASCADE,
  FOREIGN KEY (user_id) REFERENCES usuarios(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS pluggy_transactions (
  id VARCHAR(64) PRIMARY KEY,
  item_id VARCHAR(64) NOT NULL,
  account_id VARCHAR(64) NOT NULL,
  user_id INT NOT NULL,
  transaction_date DATE NOT NULL,
  description VARCHAR(500) NOT NULL,
  amount DECIMAL(15,2) NOT NULL,
  direction VARCHAR(10) NOT NULL,
  status VARCHAR(20) NOT NULL,
  payment_method VARCHAR(30) NULL,
  recipient_name VARCHAR(200) NULL,
  recipient_document VARCHAR(40) NULL,
  provider_id VARCHAR(160) NULL,
  operation_type VARCHAR(80) NULL,
  user_category VARCHAR(50) NULL,
  auto_ignored TINYINT(1) NOT NULL DEFAULT 0,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_pt_user_date (user_id, transaction_date),
  INDEX idx_pt_account (account_id),
  FOREIGN KEY (item_id) REFERENCES pluggy_items(id) ON DELETE CASCADE,
  FOREIGN KEY (account_id) REFERENCES pluggy_accounts(id) ON DELETE CASCADE,
  FOREIGN KEY (user_id) REFERENCES usuarios(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS pluggy_rules (
  id INT AUTO_INCREMENT PRIMARY KEY,
  user_id INT NOT NULL,
  gasto_id INT NOT NULL,
  recipient_name VARCHAR(200) NOT NULL,
  recipient_document VARCHAR(40) DEFAULT '',
  tolerance DECIMAL(12,2) NOT NULL DEFAULT 0,
  UNIQUE KEY uq_pr_gasto (gasto_id),
  FOREIGN KEY (gasto_id) REFERENCES gastos(id) ON DELETE CASCADE,
  FOREIGN KEY (user_id) REFERENCES usuarios(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS pluggy_matches (
  id INT AUTO_INCREMENT PRIMARY KEY,
  user_id INT NOT NULL,
  gasto_id INT NOT NULL,
  mes_ref CHAR(7) NOT NULL,
  transaction_id VARCHAR(64) NOT NULL,
  source VARCHAR(10) NOT NULL,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_pm_transaction (transaction_id),
  INDEX idx_pm_gasto_mes (gasto_id, mes_ref),
  FOREIGN KEY (user_id) REFERENCES usuarios(id) ON DELETE CASCADE,
  FOREIGN KEY (gasto_id) REFERENCES gastos(id) ON DELETE CASCADE,
  FOREIGN KEY (transaction_id) REFERENCES pluggy_transactions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS pluggy_webhook_events (
  event_id VARCHAR(80) PRIMARY KEY,
  item_id VARCHAR(64) NULL,
  event_name VARCHAR(80) NOT NULL,
  payload JSON NOT NULL,
  state VARCHAR(20) NOT NULL DEFAULT 'queued',
  attempts INT NOT NULL DEFAULT 0,
  next_attempt DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_error VARCHAR(300) NULL,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_pwe_queue (state, next_attempt)
);
