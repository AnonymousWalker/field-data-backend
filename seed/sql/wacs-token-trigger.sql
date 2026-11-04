-- Idempotent equivalent of repos/wacs/token-trigger.sql (the upstream file wraps CREATE TRIGGER
-- in an IF block, which MariaDB rejects with ERROR 1303). Gives scopeless API tokens
-- (e.g. from BTT-Writer) write:repository,write:user, matching production.
DROP TRIGGER IF EXISTS before_insert_access_token;
DELIMITER //
CREATE TRIGGER before_insert_access_token
BEFORE INSERT ON access_token
FOR EACH ROW
BEGIN
    IF NEW.scope IS NULL OR NEW.scope = '' THEN
        SET NEW.scope = 'write:repository,write:user';
    END IF;
END//
DELIMITER ;
