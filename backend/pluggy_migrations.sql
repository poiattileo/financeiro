ALTER TABLE pluggy_items ADD COLUMN IF NOT EXISTS source VARCHAR(20) NOT NULL DEFAULT 'widget';
ALTER TABLE pluggy_transactions ADD COLUMN IF NOT EXISTS auto_ignored TINYINT(1) NOT NULL DEFAULT 0;
ALTER TABLE pluggy_transactions ADD COLUMN IF NOT EXISTS user_category VARCHAR(50) NULL;
ALTER TABLE pluggy_transactions MODIFY COLUMN payment_method VARCHAR(80) NULL;
ALTER TABLE pluggy_items ADD COLUMN IF NOT EXISTS display_name VARCHAR(160) NULL;
ALTER TABLE pluggy_transactions ADD COLUMN IF NOT EXISTS bill_id VARCHAR(64) NULL;
ALTER TABLE pluggy_transactions ADD COLUMN IF NOT EXISTS bill_forecast_month CHAR(7) NULL;
