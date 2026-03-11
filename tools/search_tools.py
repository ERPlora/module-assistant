"""
Unified Search Tool — always available (hub core).

Provides free-text search across installed modules without requiring
the AI to call module-specific list tools first.

Supported modules:
  inventory     — Products (name, SKU, EAN-13, description)
  customers     — Customers (name, email, phone)
  sales         — Sales (sale_number, customer name)
  orders        — Orders (order_number, table name)
  reservations  — Reservations (guest_name, guest_phone)
  employees / staff — LocalUsers (name, email)
"""
import logging

from assistant.tools import AssistantTool, register_tool

logger = logging.getLogger(__name__)

# Map of canonical module name → aliases
_MODULE_ALIASES = {
    'inventory': ['inventory', 'products', 'product', 'catalog', 'catalogue'],
    'customers': ['customers', 'customer', 'clients', 'client'],
    'sales': ['sales', 'sale', 'tickets', 'ticket'],
    'orders': ['orders', 'order'],
    'reservations': ['reservations', 'reservation', 'booking', 'bookings'],
    'employees': ['employees', 'employee', 'staff', 'users', 'user', 'team'],
}


def _resolve_module(name: str) -> str | None:
    """Return canonical module name or None if unrecognised."""
    name = name.strip().lower()
    for canonical, aliases in _MODULE_ALIASES.items():
        if name in aliases:
            return canonical
    return None


def _is_installed(module_id: str) -> bool:
    """Return True if the module directory exists in MODULES_DIR."""
    try:
        from django.conf import settings
        from pathlib import Path
        modules_dir = Path(settings.MODULES_DIR)
        return (modules_dir / module_id).is_dir()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Per-module search helpers
# ---------------------------------------------------------------------------

def _search_inventory(query: str, hub_id, limit: int) -> list[dict]:
    from django.db.models import Q
    from inventory.models import Product

    qs = Product.objects.filter(
        hub_id=hub_id, is_deleted=False, is_active=True,
    ).filter(
        Q(name__icontains=query) |
        Q(sku__icontains=query) |
        Q(ean13__icontains=query) |
        Q(description__icontains=query)
    ).select_related('tax_class').prefetch_related('categories')[:limit]

    results = []
    for p in qs:
        results.append({
            'id': str(p.id),
            'name': p.name,
            'sku': p.sku,
            'ean13': p.ean13 or '',
            'price': str(p.price),
            'stock': p.stock_quantity if hasattr(p, 'stock_quantity') else None,
            'category': ', '.join(c.name for c in p.categories.all()),
            'is_active': p.is_active,
        })
    return results


def _search_customers(query: str, hub_id, limit: int) -> list[dict]:
    from django.db.models import Q
    from customers.models import Customer

    qs = Customer.objects.filter(
        hub_id=hub_id, is_deleted=False,
    ).filter(
        Q(name__icontains=query) |
        Q(email__icontains=query) |
        Q(phone__icontains=query)
    )[:limit]

    results = []
    for c in qs:
        results.append({
            'id': str(c.id),
            'name': c.name,
            'email': c.email or '',
            'phone': c.phone or '',
            'city': c.city or '',
            'is_active': c.is_active if hasattr(c, 'is_active') else True,
        })
    return results


def _search_sales(query: str, hub_id, limit: int) -> list[dict]:
    from django.db.models import Q
    from sales.models import Sale

    qs = Sale.objects.filter(
        hub_id=hub_id, is_deleted=False,
    ).filter(
        Q(sale_number__icontains=query) |
        Q(customer_name__icontains=query)
    ).order_by('-created_at')[:limit]

    results = []
    for s in qs:
        results.append({
            'id': str(s.id),
            'sale_number': s.sale_number,
            'status': s.status,
            'total': str(s.total) if hasattr(s, 'total') else '',
            'customer_name': s.customer_name or '',
            'created_at': s.created_at.strftime('%Y-%m-%d %H:%M') if s.created_at else '',
        })
    return results


def _search_orders(query: str, hub_id, limit: int) -> list[dict]:
    from django.db.models import Q
    from orders.models import Order

    qs = Order.objects.filter(
        hub_id=hub_id, is_deleted=False,
    ).filter(
        Q(order_number__icontains=query) |
        Q(table__name__icontains=query)
    ).select_related('table').order_by('-created_at')[:limit]

    results = []
    for o in qs:
        results.append({
            'id': str(o.id),
            'order_number': o.order_number,
            'status': o.status,
            'table': o.table.display_name if o.table else '',
            'total': str(o.total) if hasattr(o, 'total') else '',
            'created_at': o.created_at.strftime('%Y-%m-%d %H:%M') if o.created_at else '',
        })
    return results


def _search_reservations(query: str, hub_id, limit: int) -> list[dict]:
    from django.db.models import Q
    from reservations.models import Reservation

    qs = Reservation.objects.filter(
        hub_id=hub_id, is_deleted=False,
    ).filter(
        Q(guest_name__icontains=query) |
        Q(guest_phone__icontains=query) |
        Q(date__icontains=query)
    ).order_by('date', 'time')[:limit]

    results = []
    for r in qs:
        results.append({
            'id': str(r.id),
            'guest_name': r.guest_name,
            'guest_phone': r.guest_phone or '',
            'date': str(r.date),
            'time': r.time.strftime('%H:%M') if r.time else '',
            'party_size': r.party_size if hasattr(r, 'party_size') else None,
            'status': r.status if hasattr(r, 'status') else '',
        })
    return results


def _search_employees(query: str, hub_id, limit: int) -> list[dict]:
    from django.db.models import Q
    from apps.accounts.models import LocalUser

    qs = LocalUser.objects.filter(
        hub_id=hub_id, is_deleted=False, is_active=True,
    ).filter(
        Q(name__icontains=query) |
        Q(email__icontains=query)
    )[:limit]

    results = []
    for u in qs:
        results.append({
            'id': str(u.id),
            'name': u.name,
            'email': u.email or '',
            'role': u.get_role_name() if hasattr(u, 'get_role_name') else '',
            'is_active': u.is_active,
        })
    return results


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_SEARCH_HANDLERS = {
    'inventory': _search_inventory,
    'customers': _search_customers,
    'sales': _search_sales,
    'orders': _search_orders,
    'reservations': _search_reservations,
    'employees': _search_employees,
}

_MODULE_INSTALL_IDS = {
    'inventory': 'inventory',
    'customers': 'customers',
    'sales': 'sales',
    'orders': 'orders',
    'reservations': 'reservations',
    'employees': None,  # Core Hub — always available
}


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

@register_tool
class Search(AssistantTool):
    name = 'search'
    description = (
        "Search for records across different modules using a free-text query. "
        "Use this to quickly find products, customers, sales, orders, reservations, "
        "or employees without loading all records. "
        "Supported modules: inventory, customers, sales, orders, reservations, employees (staff)."
    )
    short_description = (
        "Free-text search across modules. "
        "module: inventory | customers | sales | orders | reservations | employees. "
        "Returns up to `limit` matching records."
    )
    module_id = None  # Hub core — always available
    parameters = {
        "type": "object",
        "properties": {
            "module": {
                "type": "string",
                "description": (
                    "Which module to search in. "
                    "Accepted values: inventory, customers, sales, orders, reservations, employees (or staff)."
                ),
            },
            "query": {
                "type": "string",
                "description": "Free-text search string (name, number, phone, email, barcode, …).",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of results to return (default: 10, max: 50).",
            },
        },
        "required": ["module", "query", "limit"],
        "additionalProperties": False,
    }
    examples = [
        {"module": "inventory", "query": "café solo", "limit": 10},
        {"module": "customers", "query": "García", "limit": 5},
        {"module": "sales", "query": "S-2026", "limit": 10},
        {"module": "reservations", "query": "Martínez", "limit": 10},
    ]

    def execute(self, args: dict, request) -> dict:
        raw_module = args.get('module', '')
        query = args.get('query', '').strip()
        limit = min(int(args.get('limit') or 10), 50)

        # Validate inputs
        if not query:
            return {"error": "query cannot be empty"}

        canonical = _resolve_module(raw_module)
        if canonical is None:
            supported = ', '.join(sorted(_MODULE_ALIASES.keys()))
            return {
                "error": f"Unknown module '{raw_module}'. Supported: {supported}.",
                "supported_modules": sorted(_MODULE_ALIASES.keys()),
            }

        # Check that the module is installed (employees always available)
        install_id = _MODULE_INSTALL_IDS.get(canonical)
        if install_id and not _is_installed(install_id):
            return {
                "error": f"Module '{canonical}' is not installed on this hub.",
                "module": canonical,
                "results": [],
                "count": 0,
            }

        hub_id = request.session.get('hub_id')
        handler = _SEARCH_HANDLERS[canonical]

        try:
            results = handler(query, hub_id, limit)
            return {
                "module": canonical,
                "query": query,
                "results": results,
                "count": len(results),
            }
        except Exception as e:
            logger.warning(f"[ASSISTANT] search tool error (module={canonical}): {e}")
            return {
                "error": f"Search failed for module '{canonical}': {str(e)}",
                "module": canonical,
                "results": [],
                "count": 0,
            }
