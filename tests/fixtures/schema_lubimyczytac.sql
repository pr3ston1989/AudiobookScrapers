CREATE TABLE author_repair_progress (
    book_id      INTEGER PRIMARY KEY,
    external_id  INTEGER,
    status       TEXT,
    old_authors  TEXT,
    new_authors  TEXT,
    source       TEXT,
    skip_reason  TEXT,
    checked_at   TEXT
);

CREATE TABLE authors (
	id INTEGER NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (name)
);

CREATE TABLE book_authors (
	book_id INTEGER NOT NULL, 
	author_id INTEGER NOT NULL, 
	role VARCHAR(50), 
	PRIMARY KEY (book_id, author_id), 
	FOREIGN KEY(book_id) REFERENCES books (id), 
	FOREIGN KEY(author_id) REFERENCES authors (id)
);

CREATE TABLE book_categories (
	book_id INTEGER NOT NULL, 
	category_id INTEGER NOT NULL, 
	PRIMARY KEY (book_id, category_id), 
	FOREIGN KEY(book_id) REFERENCES books (id), 
	FOREIGN KEY(category_id) REFERENCES categories (id)
);

CREATE TABLE books (
	id INTEGER NOT NULL, 
	external_id INTEGER NOT NULL, 
	slug VARCHAR(255) NOT NULL, 
	url VARCHAR(500) NOT NULL, 
	type VARCHAR(20) NOT NULL, 
	title VARCHAR(500) NOT NULL, 
	original_title VARCHAR(500), 
	description TEXT, 
	isbn VARCHAR(20), 
	language VARCHAR(50), 
	pages INTEGER, 
	duration_minutes INTEGER, 
	release_date VARCHAR(50), 
	premiere_date VARCHAR(50), 
	format VARCHAR(100), 
	translator VARCHAR(255), 
	volume_number VARCHAR(20), 
	avg_rating FLOAT, 
	publisher_id INTEGER, 
	series_id INTEGER, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (url), 
	FOREIGN KEY(publisher_id) REFERENCES publishers (id), 
	FOREIGN KEY(series_id) REFERENCES series (id)
);

CREATE TABLE categories (
	id INTEGER NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (name)
);

CREATE TABLE covers (
	id INTEGER NOT NULL, 
	book_id INTEGER NOT NULL, 
	source_url VARCHAR(500) NOT NULL, 
	local_path VARCHAR(500) NOT NULL, 
	width INTEGER, 
	height INTEGER, 
	size_bytes INTEGER, 
	sha256 VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (book_id), 
	FOREIGN KEY(book_id) REFERENCES books (id)
);

CREATE TABLE publishers (
	id INTEGER NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (name)
);

CREATE TABLE reviews (
	id INTEGER NOT NULL, 
	book_id INTEGER NOT NULL, 
	username VARCHAR(255), 
	rating INTEGER, 
	full_text TEXT NOT NULL, 
	is_featured BOOLEAN NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(book_id) REFERENCES books (id)
);

CREATE TABLE scrape_errors (
	id INTEGER NOT NULL, 
	url VARCHAR(500) NOT NULL, 
	error_msg TEXT NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE scrape_queue (
	id INTEGER NOT NULL, 
	url VARCHAR(500) NOT NULL, 
	type VARCHAR(50) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	priority INTEGER NOT NULL, 
	retry_count INTEGER NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE series (
	id INTEGER NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (name)
);

CREATE UNIQUE INDEX ix_books_external_id ON books (external_id);

CREATE INDEX ix_scrape_queue_status ON scrape_queue (status);

CREATE UNIQUE INDEX ix_scrape_queue_url ON scrape_queue (url);
