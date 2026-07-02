# Connect to MySQL as root
mysql -u root -p

# Run these commands:
CREATE DATABASE zenflow CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'zenflow'@'localhost' IDENTIFIED BY 'strong_pass';
GRANT ALL PRIVILEGES ON zenflow.* TO 'zenflow'@'localhost';
FLUSH PRIVILEGES;
EXIT;