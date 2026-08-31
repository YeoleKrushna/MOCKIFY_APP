MOCKIFY SUPER ADMIN / ANALYTICS UPGRADE

Files:
- database.py -> replace existing database.py
- auth.py -> replace existing auth.py
- admin.py -> replace existing admin.py
- analytics.py -> add new file
- app.py -> replace existing app.py
- super_admin.html -> add to project root
- public_stats.js -> add to project root

Integration:
1. Put analytics.py, super_admin.html and public_stats.js in D:\Mockify.
2. Replace database.py, auth.py, admin.py and app.py with the supplied versions.
3. Keep your existing .env secrets.
4. Start with: python app.py
5. Log in through the existing admin login. The first existing admin account is automatically marked as the Super Admin by init_db().
6. Open http://127.0.0.1:5000/super-admin.html

Public stats:
Add this to index.html wherever you want a small public stats section:

<div class="mockify-live-stats">
  <strong><span id="public-users-count">0</span></strong> learners
  <span>·</span>
  <strong><span id="public-mocks-count">0</span></strong> mocks
  <span>·</span>
  <strong><span id="public-tests-count">0</span></strong> completed tests
  <span>·</span>
  <strong><span id="public-active-count">0</span></strong> active now
</div>

Add before </body>:
<script src="/public_stats.js"></script>

Security notes:
- Super Admin endpoints require session + is_super_admin.
- OTP plaintext values are never stored.
- OTP audit records store recipient, status, timestamp, IP and Brevo message ID.
- Brevo dashboard remains the authoritative place for delivered/bounced/open status.
- Change the existing Super Admin password before public deployment.
