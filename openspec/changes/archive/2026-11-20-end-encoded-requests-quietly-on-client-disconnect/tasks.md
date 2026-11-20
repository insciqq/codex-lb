## 1. Middleware

- [x] 1.1 Catch `ClientDisconnect` around the encoded-body drain and return
- [x] 1.2 Log the aborted upload at debug level

## 2. Tests

- [x] 2.1 The middleware returns without calling the route on a disconnect
- [x] 2.2 Through a FastAPI app with the ingress middlewares, nothing escapes and a debug entry is logged
