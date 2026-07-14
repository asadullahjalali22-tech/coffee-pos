# Coffee Shop POS – Version 1

A simple Docker-based coffee shop ordering system.

## Included

- Cashier dashboard
- Product selection and cart
- Order creation
- Kitchen dashboard
- Order status workflow
- Admin product list
- MySQL database
- Flask + Gunicorn
- NGINX reverse proxy
- Docker Compose

## Start the project

```bash
cd coffee-pos
docker compose up -d --build
```

Open:

- Home: http://SERVER-IP:8080
- Cashier: http://SERVER-IP:8080/cashier
- Kitchen: http://SERVER-IP:8080/kitchen
- Admin: http://SERVER-IP:8080/admin/products
- Health: http://SERVER-IP:8080/health

## Test

```bash
docker compose ps
curl http://localhost:8080/health
```

Expected health response:

```json
{"database":"connected","status":"ok"}
```

## Stop

```bash
docker compose down
```

Keep database data:

```bash
docker compose down
```

Delete database data:

```bash
docker compose down -v
```

## Next phase

- Add/edit/delete products
- User login and roles
- Customer QR ordering
- Payment workflow
- Receipt printing
- Inventory
- Sales reports
