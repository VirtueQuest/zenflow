-- seed_data.sql
-- Insert initial skills (if not already inserted)
INSERT IGNORE INTO skills (name, emoji) VALUES
    ('Swedish Massage', '🌿'),
    ('Deep Tissue', '💪'),
    ('Thai Massage', '🧘'),
    ('Hot Stone', '🪨'),
    ('Aromatherapy', '🌸'),
    ('Reflexology', '🦶'),
    ('Facial', '✨'),
    ('Prenatal', '🤱'),
    ('Lymphatic Drainage', '💧'),
    ('Acupressure', '🎯');

-- Insert a test user
INSERT IGNORE INTO users (email, first_name, last_name, password_hash, account_type, token_balance)
VALUES (
    'test@zenflow.sg',
    'Test',
    'User',
    '$2b$12$KIXoKxvXq5Xx5Xx5Xx5Xx.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx',  -- password: Test1234!
    'customer',
    10
);

-- Insert a test professional user
INSERT IGNORE INTO users (email, first_name, last_name, password_hash, account_type, token_balance)
VALUES (
    'pro@zenflow.sg',
    'Pro',
    'Therapist',
    '$2b$12$KIXoKxvXq5Xx5Xx5Xx5Xx.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx',  -- password: Test1234!
    'professional',
    100
);

-- Insert a test professional profile
INSERT IGNORE INTO professionals (user_id, display_name, title, hourly_rate, location, is_available)
VALUES (
    (SELECT id FROM users WHERE email = 'pro@zenflow.sg'),
    'Sarah Wellness',
    'Licensed Massage Therapist',
    85.00,
    'Singapore',
    1
);

-- Link skills to professional
INSERT IGNORE INTO professional_skills (professional_id, skill_id)
SELECT 
    (SELECT id FROM professionals WHERE display_name = 'Sarah Wellness'),
    id
FROM skills 
WHERE name IN ('Swedish Massage', 'Deep Tissue', 'Aromatherapy');