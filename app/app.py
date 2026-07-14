import io
import os
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
    url_for,
)
from mysql.connector import Error

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


@app.get("/")
def home():
    return render_template("home.html")


@app.get("/health")
def health():
    try:
        connection = get_db()
        connection.close()
        return jsonify(status="ok", database="connected"), 200
    except Error as exc:
        return jsonify(status="error", database="disconnected", detail=str(exc)), 500


@app.get("/cashier")
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

    cursor.close()
    connection.close()

    return render_template(
        "cashier.html",
        table_groups=table_groups,
        counter_orders=counter_orders,
        paid_history=paid_history,
        open_table_total=open_table_total,
        counter_total=counter_total,
    )


def validate_payment_method(payment_method):
    if payment_method not in {"cash", "card"}:
        raise ValueError("Invalid payment method.")


@app.post("/cashier/tables/<int:table_id>/pay")
def pay_table_orders(table_id):
    payment_method = request.form.get("payment_method", "").strip().lower()

    try:
        validate_payment_method(payment_method)
    except ValueError as exc:
        return str(exc), 400

    connection = get_db()
    cursor = connection.cursor()

    cursor.execute(
        """
        UPDATE orders
        SET payment_status = 'paid',
            payment_method = %s
        WHERE table_id = %s
          AND payment_status = 'unpaid'
          AND status <> 'cancelled'
        """,
        (payment_method, table_id),
    )

    connection.commit()
    cursor.close()
    connection.close()
    return redirect(url_for("cashier"))


@app.post("/cashier/orders/<int:order_id>/pay")
def pay_counter_order(order_id):
    payment_method = request.form.get("payment_method", "").strip().lower()

    try:
        validate_payment_method(payment_method)
    except ValueError as exc:
        return str(exc), 400

    connection = get_db()
    cursor = connection.cursor()

    cursor.execute(
        """
        UPDATE orders
        SET payment_status = 'paid',
            payment_method = %s
        WHERE id = %s
          AND table_id IS NULL
          AND payment_status = 'unpaid'
          AND status <> 'cancelled'
        """,
        (payment_method, order_id),
    )

    connection.commit()
    cursor.close()
    connection.close()
    return redirect(url_for("cashier"))


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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
