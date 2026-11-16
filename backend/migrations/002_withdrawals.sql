-- 002 误采作业撤回与影响追踪
-- 兼容已有库：新表/新列采用 IF NOT EXISTS / 动态加列（MySQL 8）

ALTER TABLE `jobs`
  ADD COLUMN `withdrawal_id` int NULL;

ALTER TABLE `mining_runs`
  ADD COLUMN `quarantine` varchar(20) NOT NULL DEFAULT 'cleared',
  ADD COLUMN `last_withdrawal_id` int NULL;

ALTER TABLE `association_rules`
  ADD COLUMN `status` varchar(20) NOT NULL DEFAULT 'active';

ALTER TABLE `kits`
  ADD COLUMN `invalidated_by` int NULL,
  ADD COLUMN `invalid_reason` text;

CREATE TABLE IF NOT EXISTS `withdrawals` (
  `id` int NOT NULL AUTO_INCREMENT,
  `code` varchar(60) NOT NULL,
  `job_id` int NULL,
  `reason` text,
  `state` varchar(20) NOT NULL DEFAULT 'accepted',
  `impact` json NULL,
  `reconciliation` json NULL,
  `error` text,
  `processed_at` varchar(40) NULL,
  `created` varchar(40) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_withdrawal_code_state` (`code`,`state`),
  KEY `fk_withdrawal_job` (`job_id`),
  CONSTRAINT `fk_withdrawal_job` FOREIGN KEY (`job_id`) REFERENCES `jobs` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `corrections` (
  `id` int NOT NULL AUTO_INCREMENT,
  `withdrawal_id` int NOT NULL,
  `target_type` varchar(20) NOT NULL,
  `target_id` int NOT NULL,
  `state` varchar(20) NOT NULL DEFAULT 'pending',
  `detail` json NULL,
  `attempts` int NOT NULL DEFAULT 0,
  `error` text,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_correction_target` (`withdrawal_id`,`target_type`,`target_id`),
  KEY `fk_correction_withdrawal` (`withdrawal_id`),
  CONSTRAINT `fk_correction_withdrawal` FOREIGN KEY (`withdrawal_id`) REFERENCES `withdrawals` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

ALTER TABLE `jobs`
  ADD CONSTRAINT `fk_job_withdrawal` FOREIGN KEY (`withdrawal_id`) REFERENCES `withdrawals` (`id`);

ALTER TABLE `kits`
  ADD CONSTRAINT `fk_kit_invalidated_by` FOREIGN KEY (`invalidated_by`) REFERENCES `withdrawals` (`id`);
