import io
import os
import uuid
from datetime import date
from functools import wraps
from decimal import Decimal, InvalidOperation

import mysql.connector
import qrcode
from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from mysql.connector import Error
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "development-secret")


def get_db():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST", "mysql"),
        port=int(os.getenv("DB_PORT", "3306")),
        database=os.getenv("DB_NAME", "coffee_pos"),
        user=os.getenv("DB_USER", "coffee_user"),
        password=os.getenv("DB_PASSWORD", "coffee_password"),
    )


_schema_ready = False


def ensure_schema():
    global _schema_ready
    if _schema_ready:
        return
    connection = get_db()
    cursor = connection.cursor()
    statements = [
        """CREATE TABLE IF NOT EXISTS users (
            id INT AUTO_INCREMENT PRIMARY KEY,
            username VARCHAR(80) NOT NULL UNIQUE,
            password_hash VARCHAR(255) NOT NULL,
            role ENUM('admin','cashier','kitchen') NOT NULL,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS service_requests (
            id INT AUTO_INCREMENT PRIMARY KEY,
            table_id INT NOT NULL,
            request_type ENUM('waiter','bill') NOT NULL,
            status ENUM('open','completed') NOT NULL DEFAULT 'open',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP NULL,
            FOREIGN KEY (table_id) REFERENCES cafe_tables(id)
        )""",
        """CREATE TABLE IF NOT EXISTS payments (
            id VARCHAR(36) PRIMARY KEY,
            table_id INT NULL,
            method ENUM('cash','card') NOT NULL,
            subtotal DECIMAL(10,2) NOT NULL,
            discount_amount DECIMAL(10,2) NOT NULL DEFAULT 0,
            vat_rate DECIMAL(5,2) NOT NULL DEFAULT 19.00,
            total DECIMAL(10,2) NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (table_id) REFERENCES cafe_tables(id)
        )""",
        """CREATE TABLE IF NOT EXISTS payment_orders (
            payment_id VARCHAR(36) NOT NULL,
            order_id INT NOT NULL,
            PRIMARY KEY (payment_id, order_id),
            FOREIGN KEY (payment_id) REFERENCES payments(id) ON DELETE CASCADE,
            FOREIGN KEY (order_id) REFERENCES orders(id)
        )""",
    ]
    for statement in statements:
        cursor.execute(statement)
    for username, password, role in [
        ('admin', 'admin123', 'admin'),
        ('cashier', 'cashier123', 'cashier'),
        ('kitchen', 'kitchen123', 'kitchen'),
    ]:
        cursor.execute("SELECT id FROM users WHERE username=%s", (username,))
        if not cursor.fetchone():
            cursor.execute(
                "INSERT INTO users (username,password_hash,role) VALUES (%s,%s,%s)",
                (username, generate_password_hash(password), role),
            )
    connection.commit()
    cursor.close()
    connection.close()
    _schema_ready = True


def login_required(*roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            ensure_schema()
            if not session.get('user_id'):
                return redirect(url_for('login', next=request.path))
            if roles and session.get('role') not in roles:
                return 'Access denied', 403
            return view(*args, **kwargs)
        return wrapped
    return decorator


@app.context_processor
def inject_staff():
    return {'staff_user': session.get('username'), 'staff_role': session.get('role')}


@app.route('/login', methods=['GET','POST'])
def login():
    ensure_schema()
    error = None
    if request.method == 'POST':
        username = request.form.get('username','').strip()
        password = request.form.get('password','')
        connection = get_db()
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT * FROM users WHERE username=%s AND active=TRUE", (username,))
        user = cursor.fetchone()
        cursor.close(); connection.close()
        if user and check_password_hash(user['password_hash'], password):
            session.clear()
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['role'] = user['role']
            destination = request.args.get('next')
            if not destination:
                destination = {'admin':'/admin/products','cashier':'/cashier','kitchen':'/kitchen'}[user['role']]
            return redirect(destination)
        error = 'Invalid username or password.'
    return render_template('login.html', error=error)


@app.get('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.get("/")
def home():
    return render_template("home.html")


@app.get("/health")
def health():
    try:
        ensure_schema()
        connection = get_db()
        connection.close()
        return jsonify(status="ok", database="connected"), 200
    except Error as exc:
        return jsonify(status="error", database="disconnected", detail=str(exc)), 500


@app.get("/cashier")
@login_required("admin", "cashier")
def cashier():
    connection = get_db()
    cursor = connection.cursor(dictionary=True)

    # Every unpaid, non-cancelled order remains open until the cashier receives payment.
    cursor.execute(
        """
        SELECT
            o.id,
            o.table_id,
            o.order_type,
            o.status,
            o.payment_status,
            o.customer_comment,
            o.total,
            o.created_at,
            t.table_number
        FROM orders o
        LEFT JOIN cafe_tables t ON t.id = o.table_id
        WHERE o.payment_status = 'unpaid'
          AND o.status <> 'cancelled'
        ORDER BY
            CASE WHEN o.table_id IS NULL THEN 1 ELSE 0 END,
            t.table_number,
            o.created_at ASC
        """
    )
    unpaid_orders = cursor.fetchall()

    for order in unpaid_orders:
        cursor.execute(
            """
            SELECT
                p.name,
                oi.quantity,
                oi.unit_price,
                oi.line_total
            FROM order_items oi
            JOIN products p ON p.id = oi.product_id
            WHERE oi.order_id = %s
            ORDER BY oi.id
            """,
            (order["id"],),
        )
        order["items"] = cursor.fetchall()

    table_groups_by_id = {}
    counter_orders = []

    for order in unpaid_orders:
        if order["table_id"] is None:
            counter_orders.append(order)
            continue

        table_id = order["table_id"]
        if table_id not in table_groups_by_id:
            table_groups_by_id[table_id] = {
                "table_id": table_id,
                "table_number": order["table_number"],
                "orders": [],
                "total": Decimal("0.00"),
            }

        table_groups_by_id[table_id]["orders"].append(order)
        table_groups_by_id[table_id]["total"] += Decimal(order["total"])

    table_groups = list(table_groups_by_id.values())

    cursor.execute(
        """
        SELECT
            o.id,
            o.table_id,
            o.order_type,
            o.payment_method,
            o.total,
            o.created_at,
            t.table_number
        FROM orders o
        LEFT JOIN cafe_tables t ON t.id = o.table_id
        WHERE o.payment_status = 'paid'
        ORDER BY o.id DESC
        LIMIT 30
        """
    )
    paid_history = cursor.fetchall()

    open_table_total = sum(
        (group["total"] for group in table_groups),
        Decimal("0.00"),
    )
    counter_total = sum(
        (Decimal(order["total"]) for order in counter_orders),
        Decimal("0.00"),
    )

    cursor.execute("""SELECT sr.id,sr.request_type,sr.created_at,t.table_number FROM service_requests sr JOIN cafe_tables t ON t.id=sr.table_id WHERE sr.status='open' ORDER BY sr.created_at""")
    service_requests = cursor.fetchall()

    cursor.close()
    connection.close()

    return render_template(
        "cashier.html",
        table_groups=table_groups,
        counter_orders=counter_orders,
        paid_history=paid_history,
        open_table_total=open_table_total,
        counter_total=counter_total,
        service_requests=service_requests,
    )


def validate_payment_method(payment_method):
    if payment_method not in {"cash", "card"}:
        raise ValueError("Invalid payment method.")


def create_payment(order_ids, table_id, method, discount_percent):
    validate_payment_method(method)
    discount_percent = max(Decimal('0'), min(Decimal(str(discount_percent or 0)), Decimal('100')))
    connection = get_db()
    cursor = connection.cursor(dictionary=True)
    placeholders = ','.join(['%s'] * len(order_ids))
    cursor.execute(f"SELECT id,total FROM orders WHERE id IN ({placeholders}) AND payment_status='unpaid'", order_ids)
    rows = cursor.fetchall()
    if not rows:
        cursor.close(); connection.close()
        raise ValueError('No unpaid orders found.')
    subtotal = sum((Decimal(row['total']) for row in rows), Decimal('0.00'))
    discount = (subtotal * discount_percent / Decimal('100')).quantize(Decimal('0.01'))
    total = subtotal - discount
    payment_id = str(uuid.uuid4())
    cursor.execute("INSERT INTO payments (id,table_id,method,subtotal,discount_amount,vat_rate,total) VALUES (%s,%s,%s,%s,%s,19.00,%s)",
                   (payment_id, table_id, method, subtotal, discount, total))
    for row in rows:
        cursor.execute("INSERT INTO payment_orders (payment_id,order_id) VALUES (%s,%s)", (payment_id,row['id']))
        cursor.execute("UPDATE orders SET payment_status='paid',payment_method=%s WHERE id=%s", (method,row['id']))
    connection.commit(); cursor.close(); connection.close()
    return payment_id


@app.post("/cashier/tables/<int:table_id>/pay")
@login_required("admin", "cashier")
def pay_table_orders(table_id):
    method=request.form.get('payment_method','').lower()
    discount=request.form.get('discount_percent','0')
    connection=get_db(); cursor=connection.cursor()
    cursor.execute("SELECT id FROM orders WHERE table_id=%s AND payment_status='unpaid' AND status<>'cancelled'",(table_id,))
    ids=[r[0] for r in cursor.fetchall()]; cursor.close(); connection.close()
    try: payment_id=create_payment(ids,table_id,method,discount)
    except (ValueError,InvalidOperation) as exc: return str(exc),400
    return redirect(url_for('receipt',payment_id=payment_id))


@app.post("/cashier/orders/<int:order_id>/pay")
@login_required("admin", "cashier")
def pay_counter_order(order_id):
    try: payment_id=create_payment([order_id],None,request.form.get('payment_method','').lower(),request.form.get('discount_percent','0'))
    except (ValueError,InvalidOperation) as exc: return str(exc),400
    return redirect(url_for('receipt',payment_id=payment_id))


@app.get('/receipt/<payment_id>')
@login_required("admin", "cashier")
def receipt(payment_id):
    connection=get_db(); cursor=connection.cursor(dictionary=True)
    cursor.execute("SELECT p.*,t.table_number FROM payments p LEFT JOIN cafe_tables t ON t.id=p.table_id WHERE p.id=%s",(payment_id,))
    payment=cursor.fetchone()
    if not payment: cursor.close(); connection.close(); return 'Receipt not found',404
    cursor.execute("""SELECT o.id,o.created_at,oi.quantity,oi.unit_price,oi.line_total,pr.name
        FROM payment_orders po JOIN orders o ON o.id=po.order_id JOIN order_items oi ON oi.order_id=o.id
        JOIN products pr ON pr.id=oi.product_id WHERE po.payment_id=%s ORDER BY o.id,oi.id""",(payment_id,))
    items=cursor.fetchall(); cursor.close(); connection.close()
    net=(Decimal(payment['total'])/(Decimal('1')+Decimal(payment['vat_rate'])/Decimal('100'))).quantize(Decimal('0.01'))
    vat=Decimal(payment['total'])-net
    return render_template('receipt.html',payment=payment,items=items,net=net,vat=vat)


@app.post("/orders")
def create_order():
    payload = request.get_json(silent=True) or {}
    items = payload.get("items", [])
    comment = (payload.get("comment") or "").strip()
    table_id = payload.get("table_id")
    order_type = "table" if table_id else "cashier"

    if not items:
        return jsonify(error="Order must contain at least one item."), 400

    connection = get_db()
    cursor = connection.cursor(dictionary=True)

    try:
        if table_id is not None:
            table_id = int(table_id)
            cursor.execute(
                "SELECT id FROM cafe_tables WHERE id = %s AND active = TRUE",
                (table_id,),
            )
            if not cursor.fetchone():
                raise ValueError("This table is not active.")

        product_ids = [int(item["product_id"]) for item in items]
        placeholders = ",".join(["%s"] * len(product_ids))
        cursor.execute(
            f"""
            SELECT id, name, price
            FROM products
            WHERE available = TRUE AND id IN ({placeholders})
            """,
            product_ids,
        )
        products = {row["id"]: row for row in cursor.fetchall()}

        total = Decimal("0.00")
        normalized_items = []

        for item in items:
            product_id = int(item["product_id"])
            quantity = int(item.get("quantity", 1))

            if quantity < 1 or product_id not in products:
                raise ValueError("Invalid product or quantity.")

            unit_price = Decimal(products[product_id]["price"])
            line_total = unit_price * quantity
            total += line_total

            normalized_items.append(
                {
                    "product_id": product_id,
                    "quantity": quantity,
                    "unit_price": unit_price,
                    "line_total": line_total,
                }
            )

        cursor.execute(
            """
            INSERT INTO orders
                (table_id, order_type, customer_comment, total)
            VALUES (%s, %s, %s, %s)
            """,
            (table_id, order_type, comment, total),
        )
        order_id = cursor.lastrowid

        for item in normalized_items:
            cursor.execute(
                """
                INSERT INTO order_items
                    (order_id, product_id, quantity, unit_price, line_total)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    order_id,
                    item["product_id"],
                    item["quantity"],
                    item["unit_price"],
                    item["line_total"],
                ),
            )

        connection.commit()
        return jsonify(order_id=order_id, total=float(total), status="new"), 201

    except (ValueError, KeyError, TypeError) as exc:
        connection.rollback()
        return jsonify(error=str(exc)), 400
    except Error as exc:
        connection.rollback()
        return jsonify(error="Database error", detail=str(exc)), 500
    finally:
        cursor.close()
        connection.close()


@app.get("/kitchen")
@login_required("admin", "kitchen")
def kitchen():
    connection = get_db()
    cursor = connection.cursor(dictionary=True)
    cursor.execute(
        """
        SELECT
            o.id,
            o.status,
            o.customer_comment,
            o.total,
            o.created_at,
            COALESCE(CONCAT('Table ', t.table_number), 'Cashier') AS table_number
        FROM orders o
        LEFT JOIN cafe_tables t ON t.id = o.table_id
        WHERE o.status IN ('new', 'preparing', 'ready')
        ORDER BY o.created_at ASC
        """
    )
    orders = cursor.fetchall()

    for order in orders:
        cursor.execute(
            """
            SELECT p.name, oi.quantity
            FROM order_items oi
            JOIN products p ON p.id = oi.product_id
            WHERE oi.order_id = %s
            """,
            (order["id"],),
        )
        order["items"] = cursor.fetchall()

    cursor.close()
    connection.close()
    return render_template("kitchen.html", orders=orders)


@app.post("/orders/<int:order_id>/status")
def update_order_status(order_id):
    new_status = request.form.get("status")
    allowed = {"new", "preparing", "ready", "completed", "cancelled"}

    if new_status not in allowed:
        return "Invalid status", 400

    connection = get_db()
    cursor = connection.cursor()
    cursor.execute("UPDATE orders SET status = %s WHERE id = %s", (new_status, order_id))
    connection.commit()
    cursor.close()
    connection.close()
    return redirect(url_for("kitchen"))


def get_categories(cursor):
    cursor.execute("SELECT id, name FROM categories ORDER BY name")
    return cursor.fetchall()


@app.get("/admin/products")
@login_required("admin")
def admin_products():
    connection = get_db()
    cursor = connection.cursor(dictionary=True)
    cursor.execute(
        """
        SELECT p.id, p.name, p.description, p.price, p.available, c.name AS category
        FROM products p
        JOIN categories c ON c.id = p.category_id
        ORDER BY c.name, p.name
        """
    )
    products = cursor.fetchall()
    cursor.close()
    connection.close()
    return render_template("admin_products.html", products=products)


@app.route("/admin/products/new", methods=["GET", "POST"])
@login_required("admin")
def admin_product_new():
    connection = get_db()
    cursor = connection.cursor(dictionary=True)
    error = None

    if request.method == "POST":
        try:
            name = request.form["name"].strip()
            description = request.form.get("description", "").strip()
            category_id = int(request.form["category_id"])
            price = Decimal(request.form["price"])
            available = 1 if request.form.get("available") == "on" else 0

            if not name:
                raise ValueError("Product name is required.")
            if price < 0:
                raise ValueError("Price cannot be negative.")

            cursor.execute(
                """
                INSERT INTO products
                    (category_id, name, description, price, available)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (category_id, name, description, price, available),
            )
            connection.commit()
            cursor.close()
            connection.close()
            return redirect(url_for("admin_products"))
        except (ValueError, InvalidOperation, Error) as exc:
            connection.rollback()
            error = str(exc)

    categories = get_categories(cursor)
    cursor.close()
    connection.close()
    return render_template(
        "admin_product_form.html",
        page_title="Add Product",
        product=None,
        categories=categories,
        error=error,
    )


@app.route("/admin/products/<int:product_id>/edit", methods=["GET", "POST"])
@login_required("admin")
def admin_product_edit(product_id):
    connection = get_db()
    cursor = connection.cursor(dictionary=True)
    error = None

    cursor.execute("SELECT * FROM products WHERE id = %s", (product_id,))
    product = cursor.fetchone()

    if not product:
        cursor.close()
        connection.close()
        return "Product not found", 404

    if request.method == "POST":
        try:
            name = request.form["name"].strip()
            description = request.form.get("description", "").strip()
            category_id = int(request.form["category_id"])
            price = Decimal(request.form["price"])
            available = 1 if request.form.get("available") == "on" else 0

            if not name:
                raise ValueError("Product name is required.")
            if price < 0:
                raise ValueError("Price cannot be negative.")

            cursor.execute(
                """
                UPDATE products
                SET category_id = %s, name = %s, description = %s,
                    price = %s, available = %s
                WHERE id = %s
                """,
                (category_id, name, description, price, available, product_id),
            )
            connection.commit()
            cursor.close()
            connection.close()
            return redirect(url_for("admin_products"))
        except (ValueError, InvalidOperation, Error) as exc:
            connection.rollback()
            error = str(exc)
            cursor.execute("SELECT * FROM products WHERE id = %s", (product_id,))
            product = cursor.fetchone()

    categories = get_categories(cursor)
    cursor.close()
    connection.close()
    return render_template(
        "admin_product_form.html",
        page_title="Edit Product",
        product=product,
        categories=categories,
        error=error,
    )


@app.post("/admin/products/<int:product_id>/toggle")
@login_required("admin")
def admin_product_toggle(product_id):
    connection = get_db()
    cursor = connection.cursor()
    cursor.execute(
        "UPDATE products SET available = NOT available WHERE id = %s",
        (product_id,),
    )
    connection.commit()
    cursor.close()
    connection.close()
    return redirect(url_for("admin_products"))


@app.post("/admin/products/<int:product_id>/delete")
@login_required("admin")
def admin_product_delete(product_id):
    connection = get_db()
    cursor = connection.cursor()
    try:
        cursor.execute("DELETE FROM products WHERE id = %s", (product_id,))
        connection.commit()
    except Error:
        connection.rollback()
        cursor.close()
        connection.close()
        return "This product is already used in an order. Disable it instead.", 400

    cursor.close()
    connection.close()
    return redirect(url_for("admin_products"))


@app.route("/admin/categories", methods=["GET", "POST"])
@login_required("admin")
def admin_categories():
    connection = get_db()
    cursor = connection.cursor(dictionary=True)
    error = None

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            error = "Category name is required."
        else:
            try:
                cursor.execute("INSERT INTO categories (name) VALUES (%s)", (name,))
                connection.commit()
                cursor.close()
                connection.close()
                return redirect(url_for("admin_categories"))
            except Error:
                connection.rollback()
                error = "Category already exists or could not be added."

    categories = get_categories(cursor)
    cursor.close()
    connection.close()
    return render_template("admin_categories.html", categories=categories, error=error)


@app.post("/admin/categories/<int:category_id>/delete")
@login_required("admin")
def admin_category_delete(category_id):
    connection = get_db()
    cursor = connection.cursor()
    try:
        cursor.execute("DELETE FROM categories WHERE id = %s", (category_id,))
        connection.commit()
    except Error:
        connection.rollback()
        cursor.close()
        connection.close()
        return "This category is used by a product and cannot be deleted.", 400

    cursor.close()
    connection.close()
    return redirect(url_for("admin_categories"))


@app.route("/admin/tables", methods=["GET", "POST"])
@login_required("admin")
def admin_tables():
    connection = get_db()
    cursor = connection.cursor(dictionary=True)
    error = None

    if request.method == "POST":
        table_number = request.form.get("table_number", "").strip()
        if not table_number:
            error = "Table number or name is required."
        else:
            try:
                cursor.execute(
                    "INSERT INTO cafe_tables (table_number, active) VALUES (%s, TRUE)",
                    (table_number,),
                )
                connection.commit()
                cursor.close()
                connection.close()
                return redirect(url_for("admin_tables"))
            except Error:
                connection.rollback()
                error = "This table already exists or could not be created."

    cursor.execute(
        """
        SELECT id, table_number, active
        FROM cafe_tables
        ORDER BY CAST(table_number AS UNSIGNED), table_number
        """
    )
    tables = cursor.fetchall()
    cursor.close()
    connection.close()
    return render_template("admin_tables.html", tables=tables, error=error)


@app.post("/admin/tables/<int:table_id>/toggle")
@login_required("admin")
def admin_table_toggle(table_id):
    connection = get_db()
    cursor = connection.cursor()
    cursor.execute(
        "UPDATE cafe_tables SET active = NOT active WHERE id = %s",
        (table_id,),
    )
    connection.commit()
    cursor.close()
    connection.close()
    return redirect(url_for("admin_tables"))


@app.post("/admin/tables/<int:table_id>/delete")
@login_required("admin")
def admin_table_delete(table_id):
    connection = get_db()
    cursor = connection.cursor()
    try:
        cursor.execute("DELETE FROM cafe_tables WHERE id = %s", (table_id,))
        connection.commit()
    except Error:
        connection.rollback()
        cursor.close()
        connection.close()
        return "This table already has orders. Disable it instead.", 400

    cursor.close()
    connection.close()
    return redirect(url_for("admin_tables"))


@app.get("/admin/tables/<int:table_id>/qr")
def table_qr(table_id):
    connection = get_db()
    cursor = connection.cursor(dictionary=True)
    cursor.execute(
        "SELECT id, table_number FROM cafe_tables WHERE id = %s",
        (table_id,),
    )
    table = cursor.fetchone()
    cursor.close()
    connection.close()

    if not table:
        return "Table not found", 404

    base_url = os.getenv("PUBLIC_BASE_URL", request.host_url.rstrip("/")).rstrip("/")
    menu_url = f"{base_url}/table/{table_id}"

    image = qrcode.make(menu_url)
    output = io.BytesIO()
    image.save(output, format="PNG")
    output.seek(0)

    return send_file(
        output,
        mimetype="image/png",
        download_name=f"table-{table['table_number']}-qr.png",
        max_age=0,
    )


@app.get("/table/<int:table_id>")
def customer_menu(table_id):
    connection = get_db()
    cursor = connection.cursor(dictionary=True)

    cursor.execute(
        """
        SELECT id, table_number
        FROM cafe_tables
        WHERE id = %s AND active = TRUE
        """,
        (table_id,),
    )
    table = cursor.fetchone()

    if not table:
        cursor.close()
        connection.close()
        return "This table is not available.", 404

    cursor.execute(
        """
        SELECT p.id, p.name, p.description, p.price, c.name AS category
        FROM products p
        JOIN categories c ON c.id = p.category_id
        WHERE p.available = TRUE
        ORDER BY c.name, p.name
        """
    )
    products = cursor.fetchall()

    cursor.close()
    connection.close()
    return render_template("customer_menu.html", table=table, products=products)



@app.get('/api/kitchen/orders')
@login_required("admin", "kitchen")
def kitchen_orders_api():
    connection=get_db(); cursor=connection.cursor(dictionary=True)
    cursor.execute("""SELECT o.id,o.status,o.customer_comment,o.created_at,
        TIMESTAMPDIFF(MINUTE,o.created_at,NOW()) waiting_minutes,
        COALESCE(CONCAT('Table ',t.table_number),'Counter') source
        FROM orders o LEFT JOIN cafe_tables t ON t.id=o.table_id
        WHERE o.status IN ('new','preparing','ready') ORDER BY o.created_at""")
    orders=cursor.fetchall()
    for order in orders:
        cursor.execute("SELECT p.name,oi.quantity FROM order_items oi JOIN products p ON p.id=oi.product_id WHERE oi.order_id=%s",(order['id'],))
        order['items']=cursor.fetchall()
        order['created_at']=order['created_at'].isoformat()
    cursor.close(); connection.close(); return jsonify(orders)


@app.get('/order/<int:order_id>/status')
def customer_order_status(order_id):
    connection=get_db(); cursor=connection.cursor(dictionary=True)
    cursor.execute("SELECT o.id,o.status,o.payment_status,o.created_at,o.table_id,t.table_number FROM orders o LEFT JOIN cafe_tables t ON t.id=o.table_id WHERE o.id=%s",(order_id,))
    order=cursor.fetchone(); cursor.close(); connection.close()
    if not order: return 'Order not found',404
    return render_template('customer_status.html',order=order)


@app.get('/api/order/<int:order_id>/status')
def customer_order_status_api(order_id):
    connection=get_db(); cursor=connection.cursor(dictionary=True)
    cursor.execute("SELECT id,status,payment_status FROM orders WHERE id=%s",(order_id,)); order=cursor.fetchone()
    cursor.close(); connection.close()
    return (jsonify(order),200) if order else (jsonify(error='Not found'),404)


@app.post('/table/<int:table_id>/request')
def table_service_request(table_id):
    request_type=(request.get_json(silent=True) or {}).get('request_type')
    if request_type not in {'waiter','bill'}: return jsonify(error='Invalid request'),400
    connection=get_db(); cursor=connection.cursor()
    cursor.execute("SELECT id FROM service_requests WHERE table_id=%s AND request_type=%s AND status='open'",(table_id,request_type))
    if not cursor.fetchone(): cursor.execute("INSERT INTO service_requests (table_id,request_type) VALUES (%s,%s)",(table_id,request_type))
    connection.commit(); cursor.close(); connection.close(); return jsonify(status='ok')


@app.post('/service-requests/<int:request_id>/complete')
@login_required("admin", "cashier")
def complete_service_request(request_id):
    connection=get_db(); cursor=connection.cursor(); cursor.execute("UPDATE service_requests SET status='completed',completed_at=NOW() WHERE id=%s",(request_id,)); connection.commit(); cursor.close(); connection.close()
    return redirect(url_for('cashier'))


@app.get('/reports/daily')
@login_required("admin")
def daily_report():
    selected=request.args.get('date',date.today().isoformat())
    connection=get_db(); cursor=connection.cursor(dictionary=True)
    cursor.execute("SELECT COUNT(*) payments,COALESCE(SUM(total),0) revenue,COALESCE(SUM(discount_amount),0) discounts FROM payments WHERE DATE(created_at)=%s",(selected,)); summary=cursor.fetchone()
    cursor.execute("SELECT method,COUNT(*) count,COALESCE(SUM(total),0) total FROM payments WHERE DATE(created_at)=%s GROUP BY method",(selected,)); methods=cursor.fetchall()
    cursor.execute("""SELECT pr.name,SUM(oi.quantity) quantity,SUM(oi.line_total) sales FROM payment_orders po JOIN payments py ON py.id=po.payment_id JOIN order_items oi ON oi.order_id=po.order_id JOIN products pr ON pr.id=oi.product_id WHERE DATE(py.created_at)=%s GROUP BY pr.id,pr.name ORDER BY quantity DESC LIMIT 10""",(selected,)); products=cursor.fetchall()
    cursor.close(); connection.close(); return render_template('daily_report.html',selected=selected,summary=summary,methods=methods,products=products)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
