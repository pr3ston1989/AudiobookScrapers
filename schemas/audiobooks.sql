CREATE TABLE authors (
	id INTEGER NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (name)
);

CREATE TABLE book_authors (
	book_id INTEGER NOT NULL, 
	author_id INTEGER NOT NULL, 
	PRIMARY KEY (book_id, author_id), 
	FOREIGN KEY(book_id) REFERENCES books (id) ON DELETE CASCADE, 
	FOREIGN KEY(author_id) REFERENCES authors (id) ON DELETE CASCADE
);

CREATE TABLE book_collections (
	book_id INTEGER NOT NULL, 
	collection_id INTEGER NOT NULL, 
	PRIMARY KEY (book_id, collection_id), 
	FOREIGN KEY(book_id) REFERENCES books (id) ON DELETE CASCADE, 
	FOREIGN KEY(collection_id) REFERENCES collections (id) ON DELETE CASCADE
);

CREATE TABLE book_narrators (
	book_id INTEGER NOT NULL, 
	narrator_id INTEGER NOT NULL, 
	PRIMARY KEY (book_id, narrator_id), 
	FOREIGN KEY(book_id) REFERENCES books (id) ON DELETE CASCADE, 
	FOREIGN KEY(narrator_id) REFERENCES narrators (id) ON DELETE CASCADE
);

CREATE TABLE book_tags (
	book_id INTEGER NOT NULL, 
	tag_id INTEGER NOT NULL, 
	PRIMARY KEY (book_id, tag_id), 
	FOREIGN KEY(book_id) REFERENCES books (id) ON DELETE CASCADE, 
	FOREIGN KEY(tag_id) REFERENCES tags (id) ON DELETE CASCADE
);

CREATE TABLE books (
	id INTEGER NOT NULL, 
	url VARCHAR(500) NOT NULL, 
	title VARCHAR(500) NOT NULL, 
	description TEXT, 
	avg_rating FLOAT, 
	rating_count INTEGER, 
	length_str VARCHAR(100), 
	duration_minutes INTEGER, 
	language VARCHAR(50), 
	type VARCHAR(100), 
	format VARCHAR(50), 
	volume_number VARCHAR(50), 
	why_worth_it TEXT, 
	cover_url VARCHAR(500), 
	cover_local_path VARCHAR(500), 
	release_date VARCHAR(50), 
	release_year INTEGER, 
	publisher_id INTEGER, 
	category_id INTEGER, 
	PRIMARY KEY (id), 
	FOREIGN KEY(publisher_id) REFERENCES publishers (id), 
	FOREIGN KEY(category_id) REFERENCES categories (id)
);

CREATE TABLE categories (
	id INTEGER NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (name)
);

CREATE TABLE collections (
	id INTEGER NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (name)
);

CREATE TABLE narrators (
	id INTEGER NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (name)
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
	username VARCHAR(255) NOT NULL, 
	rating INTEGER, 
	date VARCHAR(100), 
	text TEXT NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(book_id) REFERENCES books (id) ON DELETE CASCADE
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

CREATE TABLE tags (
	id INTEGER NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (name)
);

CREATE UNIQUE INDEX ix_books_url ON books (url);

CREATE INDEX ix_scrape_queue_status ON scrape_queue (status);

CREATE UNIQUE INDEX ix_scrape_queue_url ON scrape_queue (url);
