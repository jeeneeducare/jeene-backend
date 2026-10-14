-- The three passes the profile screen already advertises, and the store product each one
-- is on Google Play and the App Store. Those ids are permanent in both consoles: they must
-- match what was typed there exactly.
--
-- Prices are the ones drawn on the Pro cards today (₹199 / ₹499 / ₹1499), in paise, so
-- what a student is charged matches what the app has been showing all along. Change them
-- through the admin API rather than here: this file seeds a database, it does not govern
-- one, and editing it will not reprice anything already deployed.
--
-- Idempotent, so it can be re-run against a database that already has them.

INSERT INTO products (product_id, tenant_id, title, tier, amount_paise, currency,
                      duration_days, badge, sort_order, active,
                      google_product_id, apple_product_id)
VALUES
  ('pro_monthly',   'JEENE_MASTER', 'Monthly',   'pro',  19900, 'INR',  30, 'Most Popular', 1, TRUE,
   'pro_pass_30d',  'pro_pass_30d'),
  ('pro_quarterly', 'JEENE_MASTER', 'Quarterly', 'pro',  49900, 'INR',  90, 'Save 16%',     2, TRUE,
   'pro_pass_90d',  'pro_pass_90d'),
  ('pro_yearly',    'JEENE_MASTER', 'Yearly',    'pro', 149900, 'INR', 365, 'Best Value',   3, TRUE,
   'pro_pass_365d', 'pro_pass_365d')
ON CONFLICT (product_id) DO NOTHING;
