USE coffee_pos;

CREATE TABLE IF NOT EXISTS categories (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(100) NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS products (
    id INT AUTO_INCREMENT PRIMARY KEY,
    category_id INT NOT NULL,
    name VARCHAR(150) NOT NULL,
    description TEXT,
    price DECIMAL(10,2) NOT NULL,
    available BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (category_id) REFERENCES categories(id)
);

CREATE TABLE IF NOT EXISTS cafe_tables (
    id INT AUTO_INCREMENT PRIMARY KEY,
    table_number VARCHAR(20) NOT NULL UNIQUE,
    active BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS orders (
    id INT AUTO_INCREMENT PRIMARY KEY,
    table_id INT NULL,
    order_type ENUM('cashier', 'table') NOT NULL DEFAULT 'cashier',
    status ENUM('new', 'preparing', 'ready', 'completed', 'cancelled') NOT NULL DEFAULT 'new',
    payment_status ENUM('unpaid', 'paid') NOT NULL DEFAULT 'unpaid',
    payment_method ENUM('cash', 'card') NULL,
    customer_comment TEXT,
    total DECIMAL(10,2) NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (table_id) REFERENCES cafe_tables(id)
);

CREATE TABLE IF NOT EXISTS order_items (
    id INT AUTO_INCREMENT PRIMARY KEY,
    order_id INT NOT NULL,
    product_id INT NOT NULL,
    quantity INT NOT NULL DEFAULT 1,
    unit_price DECIMAL(10,2) NOT NULL,
    line_total DECIMAL(10,2) NOT NULL,
    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE,
    FOREIGN KEY (product_id) REFERENCES products(id)
);

INSERT IGNORE INTO categories (id, name) VALUES
(1, 'Coffee'),
(2, 'Tea'),
(3, 'Cake');

INSERT IGNORE INTO products (id, category_id, name, description, price, available) VALUES
(1, 1, 'Espresso', 'Strong single espresso', 2.50, TRUE),
(2, 1, 'Cappuccino', 'Espresso with steamed milk foam', 3.80, TRUE),
(3, 1, 'Latte', 'Espresso with steamed milk', 4.20, TRUE),
(4, 2, 'Black Tea', 'Classic black tea', 2.80, TRUE),
(5, 3, 'Cheesecake', 'Creamy cheesecake slice', 4.50, TRUE);

INSERT IGNORE INTO cafe_tables (id, table_number, active) VALUES
(1, '1', TRUE),
(2, '2', TRUE),
(3, '3', TRUE),
(4, '4', TRUE);
