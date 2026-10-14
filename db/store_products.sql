-- Put the store product ids on the three passes, and switch off the ₹1 test pass.
--
-- One-off, for a database that already has the passes (production does). Safe to re-run:
-- every statement sets a value rather than adding to one. Run it after
-- `backend_schema.sql` has added the columns:
--
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/store_products.sql
--
-- The ids must match Play Console and App Store Connect exactly. Neither console lets a
-- product id be changed or reused, so a typo here is fixed here, never there.

BEGIN;

UPDATE products
   SET google_product_id = 'pro_pass_30d', apple_product_id = 'pro_pass_30d', updated_at = now()
 WHERE product_id = 'pro_monthly' AND tenant_id = 'JEENE_MASTER';

UPDATE products
   SET google_product_id = 'pro_pass_90d', apple_product_id = 'pro_pass_90d', updated_at = now()
 WHERE product_id = 'pro_quarterly' AND tenant_id = 'JEENE_MASTER';

UPDATE products
   SET google_product_id = 'pro_pass_365d', apple_product_id = 'pro_pass_365d', updated_at = now()
 WHERE product_id = 'pro_yearly' AND tenant_id = 'JEENE_MASTER';

-- Created to prove Razorpay end to end with a real ₹1 charge. It has no store product, so
-- it could not be bought from the apps anyway; switched off so the paywall stops listing it.
UPDATE products SET active = FALSE, updated_at = now()
 WHERE product_id = 'pro_smoke' AND active;

SELECT product_id, amount_paise / 100 AS rupees, duration_days,
       google_product_id, apple_product_id, active
  FROM products
 ORDER BY sort_order, product_id;

COMMIT;
