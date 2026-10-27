# Changelog

## 0.1.1

- The Configure screen no longer shows "Translation error: UNCLOSED_TAG". The options text said `Bearer <token>`, and Home Assistant formats that string as an ICU message, so `<token>` was read as an unclosed tag. It now says `Bearer YOUR_TOKEN`.
- The suggested add-on URL is `http://ba3d2a6a-email-mcp:9560`, the internal hostname when the add-on is installed from this GitHub repository. A local add-on copy still uses `http://local-email-mcp:9560`.
