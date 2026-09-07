-- The three passes the profile screen already advertises.
--
-- Prices are the ones drawn on the Pro cards today (₹199 / ₹499 / ₹1499), in paise, so
-- what a student is charged matches what the app has been showing all along. Change them
-- through the admin API rather than here: this file seeds a database, it does not govern
-- one, and editing it will not reprice anything already deployed.
--
-- Idempotent, so it can be re-run against a database that already has them.

INSERT INTO products (product_id, tenant_id, title, tier, amount_paise, currency,
                      duration_days, badge, sort_order, active)
VALUES
  ('pro_monthly',   'JEENE_MASTER', 'Monthly',   'pro',  19900, 'INR',  30, 'Most Popular', 1, TRUE),
  ('pro_quarterly', 'JEENE_MASTER', 'Quarterly', 'pro',  49900, 'INR',  90, 'Save 16%',     2, TRUE),
  ('pro_yearly',    'JEENE_MASTER', 'Yearly',    'pro', 149900, 'INR', 365, 'Best Value',   3, TRUE)
ON CONFLICT (product_id) DO NOTHING;
