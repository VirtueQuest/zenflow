-- ═══════════════════════════════════════════════════════════════════
--  ZenFlow — MySQL Schema (Simplified Triggers)
--  Run: mysql -u zenflow -p zenflow < 001_mysql_schema_fixed.sql
-- ═══════════════════════════════════════════════════════════════════

SET FOREIGN_KEY_CHECKS = 0;
SET NAMES utf8mb4;

-- ─────────────────────────────────────────
--  1. USERS
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    email           VARCHAR(254) NOT NULL UNIQUE,
    first_name      VARCHAR(60) NOT NULL,
    last_name       VARCHAR(60) NOT NULL,
    password_hash   TEXT NOT NULL,
    account_type    ENUM('customer', 'professional') NOT NULL DEFAULT 'customer',
    token_balance   INT NOT NULL DEFAULT 0 CHECK (token_balance >= 0),
    lang_pref       ENUM('en', 'zh') NOT NULL DEFAULT 'en',
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    joined_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_login_at   TIMESTAMP NULL DEFAULT NULL,
    INDEX idx_users_email (email),
    INDEX idx_users_account_type (account_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ─────────────────────────────────────────
--  2. PROFESSIONALS
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS professionals (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id         BIGINT NOT NULL UNIQUE,
    display_name    VARCHAR(255) NOT NULL,
    display_name_zh VARCHAR(255) DEFAULT NULL,
    title           VARCHAR(255) NOT NULL,
    title_zh        VARCHAR(255) DEFAULT NULL,
    bio             TEXT DEFAULT NULL,
    bio_zh          TEXT DEFAULT NULL,
    location        VARCHAR(255) DEFAULT NULL,
    hourly_rate     DECIMAL(8,2) NOT NULL DEFAULT 0 CHECK (hourly_rate >= 0),
    years_exp       INT DEFAULT 0,
    gender          ENUM('Male', 'Female', 'Non-binary', 'Prefer not to say') DEFAULT NULL,
    contact_wa      VARCHAR(50) DEFAULT NULL,
    contact_wc      VARCHAR(50) DEFAULT NULL,
    video_url       TEXT DEFAULT NULL,
    emoji           VARCHAR(10) DEFAULT '🌿',
    rating_avg      DECIMAL(3,1) NOT NULL DEFAULT 0.0,
    rating_count    INT NOT NULL DEFAULT 0,
    is_available    BOOLEAN NOT NULL DEFAULT TRUE,
    is_featured     BOOLEAN NOT NULL DEFAULT FALSE,
    is_verified     BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    INDEX idx_prof_user_id (user_id),
    INDEX idx_prof_available (is_available, is_featured),
    INDEX idx_prof_rate (hourly_rate),
    INDEX idx_prof_rating (rating_avg DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ─────────────────────────────────────────
--  3. SKILLS
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS skills (
    id      INT AUTO_INCREMENT PRIMARY KEY,
    name    VARCHAR(100) NOT NULL UNIQUE,
    emoji   VARCHAR(10) DEFAULT '🌿'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT IGNORE INTO skills (name, emoji) VALUES
    ('Swedish Massage',    '🌿'),
    ('Deep Tissue',        '💪'),
    ('Thai Massage',       '🧘'),
    ('Hot Stone',          '🪨'),
    ('Aromatherapy',       '🌸'),
    ('Reflexology',        '🦶'),
    ('Facial',             '✨'),
    ('Prenatal',           '🤱'),
    ('Lymphatic Drainage', '💧'),
    ('Acupressure',        '🎯');

-- ─────────────────────────────────────────
--  4. PROFESSIONAL ↔ SKILLS
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS professional_skills (
    professional_id BIGINT NOT NULL,
    skill_id        INT NOT NULL,
    PRIMARY KEY (professional_id, skill_id),
    FOREIGN KEY (professional_id) REFERENCES professionals(id) ON DELETE CASCADE,
    FOREIGN KEY (skill_id) REFERENCES skills(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ─────────────────────────────────────────
--  5. PROFESSIONAL MEDIA
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS professional_media (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    professional_id BIGINT NOT NULL,
    media_type      ENUM('photo', 'video') NOT NULL,
    url             TEXT NOT NULL,
    s3_key          VARCHAR(255) DEFAULT NULL,
    sort_order      INT NOT NULL DEFAULT 0,
    uploaded_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (professional_id) REFERENCES professionals(id) ON DELETE CASCADE,
    INDEX idx_media_professional (professional_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ─────────────────────────────────────────
--  6. AVAILABILITY SCHEDULE
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS availability_schedule (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    professional_id BIGINT NOT NULL,
    day_of_week     TINYINT NOT NULL CHECK (day_of_week BETWEEN 0 AND 6),
    start_time      TIME NOT NULL,
    end_time        TIME NOT NULL,
    UNIQUE KEY (professional_id, day_of_week),
    FOREIGN KEY (professional_id) REFERENCES professionals(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ─────────────────────────────────────────
--  7. BLOCKED DATES
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS blocked_dates (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    professional_id BIGINT NOT NULL,
    blocked_date    DATE NOT NULL,
    reason          TEXT DEFAULT NULL,
    UNIQUE KEY (professional_id, blocked_date),
    FOREIGN KEY (professional_id) REFERENCES professionals(id) ON DELETE CASCADE,
    INDEX idx_blocked_dates (professional_id, blocked_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ─────────────────────────────────────────
--  8. BOOKINGS
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bookings (
    id               BIGINT AUTO_INCREMENT PRIMARY KEY,
    booking_ref      VARCHAR(20) NOT NULL UNIQUE,
    customer_user_id BIGINT DEFAULT NULL,
    professional_id  BIGINT NOT NULL,
    customer_name    VARCHAR(100) NOT NULL,
    contact_type     ENUM('whatsapp', 'wechat') NOT NULL,
    contact_value    VARCHAR(100) NOT NULL,
    booking_date     DATE NOT NULL,
    booking_time     VARCHAR(10) NOT NULL,
    duration_hours   SMALLINT NOT NULL DEFAULT 1 CHECK (duration_hours BETWEEN 1 AND 8),
    total_amount     DECIMAL(10,2) NOT NULL CHECK (total_amount >= 0),
    notes            TEXT DEFAULT NULL,
    status           ENUM('confirmed', 'cancelled', 'completed', 'no_show') NOT NULL DEFAULT 'confirmed',
    notif_sent_at    TIMESTAMP NULL DEFAULT NULL,
    notif_channel    VARCHAR(20) DEFAULT NULL,
    created_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (customer_user_id) REFERENCES users(id) ON DELETE SET NULL,
    FOREIGN KEY (professional_id) REFERENCES professionals(id) ON DELETE RESTRICT,
    INDEX idx_bookings_ref (booking_ref),
    INDEX idx_bookings_prof_date (professional_id, booking_date),
    INDEX idx_bookings_customer (customer_user_id),
    INDEX idx_bookings_status (status),
    INDEX idx_bookings_date (booking_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ─────────────────────────────────────────
--  9. REVIEWS
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS reviews (
    id               BIGINT AUTO_INCREMENT PRIMARY KEY,
    booking_id       BIGINT NOT NULL UNIQUE,
    professional_id  BIGINT NOT NULL,
    reviewer_user_id BIGINT DEFAULT NULL,
    rating           TINYINT NOT NULL CHECK (rating BETWEEN 1 AND 5),
    comment          TEXT DEFAULT NULL,
    is_visible       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (booking_id) REFERENCES bookings(id) ON DELETE CASCADE,
    FOREIGN KEY (professional_id) REFERENCES professionals(id) ON DELETE CASCADE,
    FOREIGN KEY (reviewer_user_id) REFERENCES users(id) ON DELETE SET NULL,
    INDEX idx_reviews_prof (professional_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ─────────────────────────────────────────
--  10. TOKEN TRANSACTIONS
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS token_transactions (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id       BIGINT NOT NULL,
    type          ENUM('purchase', 'spend', 'refund', 'bonus') NOT NULL,
    amount        INT NOT NULL,
    balance_after INT NOT NULL,
    description   TEXT DEFAULT NULL,
    ref_id        VARCHAR(50) DEFAULT NULL,
    created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    INDEX idx_token_tx_user (user_id, created_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ─────────────────────────────────────────
--  11. ADVERTISEMENTS
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS advertisements (
    id           BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id      BIGINT NOT NULL,
    ad_text      VARCHAR(100) NOT NULL,
    cta_label    VARCHAR(50) NOT NULL DEFAULT 'Book Now',
    status       ENUM('active', 'paused', 'expired', 'pending') NOT NULL DEFAULT 'active',
    days_total   INT NOT NULL CHECK (days_total > 0),
    days_left    INT NOT NULL CHECK (days_left >= 0),
    tokens_spent INT NOT NULL CHECK (tokens_spent > 0),
    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    expires_at   DATE DEFAULT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    INDEX idx_ads_status_days (status, days_left),
    INDEX idx_ads_user (user_id),
    INDEX idx_ads_expires (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ─────────────────────────────────────────
--  12. PAYMENTS
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS payments (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id         BIGINT NOT NULL,
    payment_type    ENUM('token_purchase', 'session_fee') NOT NULL,
    amount_usd      DECIMAL(10,2) NOT NULL CHECK (amount_usd > 0),
    tokens_granted  INT DEFAULT 0,
    payment_method  VARCHAR(50) DEFAULT NULL,
    stripe_pi_id    VARCHAR(100) UNIQUE DEFAULT NULL,
    stripe_status   VARCHAR(50) DEFAULT NULL,
    payment_ref     VARCHAR(50) DEFAULT NULL,
    status          ENUM('pending', 'completed', 'failed', 'refunded') NOT NULL DEFAULT 'pending',
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    INDEX idx_payments_stripe (stripe_pi_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ─────────────────────────────────────────
--  13. PASSWORD RESET TOKENS
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id         BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id    BIGINT NOT NULL,
    token_hash VARCHAR(255) NOT NULL UNIQUE,
    expires_at TIMESTAMP NOT NULL,
    used_at    TIMESTAMP NULL DEFAULT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    INDEX idx_pwd_reset_hash (token_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ─────────────────────────────────────────
--  14. NOTIFICATION LOG
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS notification_log (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    booking_ref VARCHAR(20) NOT NULL,
    channel     ENUM('whatsapp', 'wechat', 'email') NOT NULL,
    recipient   VARCHAR(255) NOT NULL,
    message     TEXT NOT NULL,
    status      ENUM('sent', 'failed', 'queued') NOT NULL,
    error_msg   TEXT DEFAULT NULL,
    sent_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_notif_ref (booking_ref)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

SET FOREIGN_KEY_CHECKS = 1;

-- ═══════════════════════════════════════════
--  TRIGGERS (with DEFINER removed)
-- ═══════════════════════════════════════════

-- IMPORTANT: Run this first if you get SUPER privilege errors:
-- SET GLOBAL log_bin_trust_function_creators = 1;

DELIMITER //

-- 1. Recalculate professional rating when review inserted
CREATE TRIGGER IF NOT EXISTS trg_rating_on_review_insert
AFTER INSERT ON reviews
FOR EACH ROW
BEGIN
    UPDATE professionals 
    SET rating_avg = COALESCE(
        (SELECT ROUND(AVG(rating), 1) FROM reviews 
         WHERE professional_id = NEW.professional_id AND is_visible = TRUE),
        0
    ),
    rating_count = COALESCE(
        (SELECT COUNT(*) FROM reviews 
         WHERE professional_id = NEW.professional_id AND is_visible = TRUE),
        0
    )
    WHERE id = NEW.professional_id;
END//

-- 2. Recalculate professional rating when review updated
CREATE TRIGGER IF NOT EXISTS trg_rating_on_review_update
AFTER UPDATE ON reviews
FOR EACH ROW
BEGIN
    UPDATE professionals 
    SET rating_avg = COALESCE(
        (SELECT ROUND(AVG(rating), 1) FROM reviews 
         WHERE professional_id = NEW.professional_id AND is_visible = TRUE),
        0
    ),
    rating_count = COALESCE(
        (SELECT COUNT(*) FROM reviews 
         WHERE professional_id = NEW.professional_id AND is_visible = TRUE),
        0
    )
    WHERE id = NEW.professional_id;
END//

-- 3. Auto-expire ads when days_left = 0
CREATE TRIGGER IF NOT EXISTS trg_expire_ad
BEFORE UPDATE ON advertisements
FOR EACH ROW
BEGIN
    IF NEW.days_left <= 0 THEN
        SET NEW.status = 'expired';
    END IF;
END//

-- 4. Deduct tokens when ad created
CREATE TRIGGER IF NOT EXISTS trg_token_spend_on_ad
AFTER INSERT ON advertisements
FOR EACH ROW
BEGIN
    DECLARE new_balance INT DEFAULT 0;
    
    UPDATE users SET token_balance = token_balance - NEW.tokens_spent
    WHERE id = NEW.user_id;
    
    SELECT token_balance INTO new_balance FROM users WHERE id = NEW.user_id;
    
    INSERT INTO token_transactions (user_id, type, amount, balance_after, description, ref_id)
    VALUES (NEW.user_id, 'spend', -NEW.tokens_spent, new_balance,
            CONCAT('Ad placement: ', LEFT(NEW.ad_text, 40)), NEW.id);
END//

-- 5. Credit tokens on payment insert
CREATE TRIGGER IF NOT EXISTS trg_token_credit_on_payment_insert
AFTER INSERT ON payments
FOR EACH ROW
BEGIN
    DECLARE new_balance INT DEFAULT 0;
    
    IF NEW.status = 'completed' AND NEW.tokens_granted > 0 THEN
        UPDATE users SET token_balance = token_balance + NEW.tokens_granted
        WHERE id = NEW.user_id;
        
        SELECT token_balance INTO new_balance FROM users WHERE id = NEW.user_id;
        
        INSERT INTO token_transactions (user_id, type, amount, balance_after, description, ref_id)
        VALUES (NEW.user_id, 'purchase', NEW.tokens_granted, new_balance,
                CONCAT('Token purchase — $', NEW.amount_usd), NEW.id);
    END IF;
END//

-- 6. Credit tokens on payment update
CREATE TRIGGER IF NOT EXISTS trg_token_credit_on_payment_update
AFTER UPDATE ON payments
FOR EACH ROW
BEGIN
    DECLARE new_balance INT DEFAULT 0;
    
    IF NEW.status = 'completed' AND NEW.tokens_granted > 0 
       AND (OLD.status != 'completed' OR OLD.status IS NULL) THEN
        UPDATE users SET token_balance = token_balance + NEW.tokens_granted
        WHERE id = NEW.user_id;
        
        SELECT token_balance INTO new_balance FROM users WHERE id = NEW.user_id;
        
        INSERT INTO token_transactions (user_id, type, amount, balance_after, description, ref_id)
        VALUES (NEW.user_id, 'purchase', NEW.tokens_granted, new_balance,
                CONCAT('Token purchase — $', NEW.amount_usd), NEW.id);
    END IF;
END//

DELIMITER ;