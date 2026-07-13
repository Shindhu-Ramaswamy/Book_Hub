"""
GET  /api/books               — list books (public, optional ?genre=&q=)
GET  /api/books/<id>          — book detail (public)
POST /api/books               — add book (librarian)
PUT  /api/books/<id>          — edit book (librarian)
DELETE /api/books/<id>        — delete book (librarian)
"""
from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt
from services.book_service import BookService

books_api = Blueprint('books_api', __name__)


def _require_role(*roles):
    claims = get_jwt()
    if claims.get('role') not in roles:
        return jsonify({'success': False, 'error': 'Forbidden.'}), 403
    return None


def _ok(data, status=200):
    return jsonify({'success': True, **data}), status


def _err(msg, status=400):
    return jsonify({'success': False, 'error': msg}), status


# ── List ──────────────────────────────────────────────────────────────
@books_api.route('', methods=['GET'])
def list_books():
    genres = request.args.getlist('genre')
    query  = request.args.get('q', '').strip()
    books  = BookService.get_all(genres=genres or None, query=query or None)
    return _ok({'books': [b.to_dict() for b in books], 'total': len(books)})


# ── Detail ────────────────────────────────────────────────────────────
@books_api.route('/<int:book_id>', methods=['GET'])
def book_detail(book_id):
    book = BookService.get_or_404(book_id)
    return _ok({'book': book.to_dict()})


# ── Create ────────────────────────────────────────────────────────────
@books_api.route('', methods=['POST'])
@jwt_required()
def create_book():
    guard = _require_role('librarian', 'admin')
    if guard:
        return guard

    body = request.get_json(silent=True) or {}
    required = ['isbn', 'title', 'author', 'genre', 'total_quantity']
    missing  = [f for f in required if not body.get(f)]
    if missing:
        return _err(f'Missing: {", ".join(missing)}')

    book, err = BookService.create(
        isbn=body['isbn'], title=body['title'], author=body['author'],
        genre=body['genre'], total_quantity=int(body['total_quantity']),
    )
    if err:
        return _err(err)
    return _ok({'message': 'Book added.', 'book': book.to_dict()}, 201)


# ── Update ────────────────────────────────────────────────────────────
@books_api.route('/<int:book_id>', methods=['PUT'])
@jwt_required()
def update_book(book_id):
    guard = _require_role('librarian', 'admin')
    if guard:
        return guard

    book = BookService.get_or_404(book_id)
    body = request.get_json(silent=True) or {}
    book = BookService.update(
        book,
        title          = body.get('title',          book.title),
        author         = body.get('author',         book.author),
        genre          = body.get('genre',           book.genre),
        total_quantity = int(body.get('total_quantity', book.total_quantity)),
    )
    return _ok({'message': 'Book updated.', 'book': book.to_dict()})


# ── Delete ────────────────────────────────────────────────────────────
@books_api.route('/<int:book_id>', methods=['DELETE'])
@jwt_required()
def delete_book(book_id):
    guard = _require_role('librarian', 'admin')
    if guard:
        return guard

    book = BookService.get_or_404(book_id)
    err  = BookService.delete(book)
    if err:
        return _err(err, 409)
    return _ok({'message': 'Book deleted.'})
