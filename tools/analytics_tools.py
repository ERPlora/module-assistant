"""
Cross-Module Analytics Tools.

Provides high-level business intelligence by aggregating data across
multiple modules in a single tool call, reducing the need for sequential
tool calls to get a business overview.
"""
import logging
from datetime import date, timedelta

from assistant.tools import AssistantTool, register_tool

logger = logging.getLogger(__name__)


@register_tool
class GetBusinessDashboard(AssistantTool):
    name = "get_business_dashboard"
    description = (
        "Get a comprehensive business overview in one call. Returns sales summary, "
        "top products/services, stock alerts, recent customers, and pending items. "
        "Only includes data from installed modules."
    )
    short_description = "Get business dashboard: sales, top products, stock alerts, pending items."
    parameters = {
        "type": "object",
        "properties": {
            "period": {
                "type": "string",
                "enum": ["today", "this_week", "this_month"],
                "description": "Time period for the dashboard (default: today)",
            },
        },
        "additionalProperties": False,
    }
    examples = [
        {"period": "today"},
        {"period": "this_month"},
    ]

    def execute(self, args, request):
        period = args.get('period', 'today')
        today = date.today()

        if period == 'this_week':
            start_date = today - timedelta(days=today.weekday())
        elif period == 'this_month':
            start_date = today.replace(day=1)
        else:
            start_date = today

        dashboard = {'period': period, 'start_date': str(start_date)}

        # Sales
        try:
            from django.db.models import Sum, Count, Avg
            from sales.models import Sale
            sales = Sale.objects.filter(
                status='completed', created_at__date__gte=start_date,
            )
            dashboard['sales'] = {
                'count': sales.count(),
                'total': str(sales.aggregate(t=Sum('total'))['t'] or 0),
                'average': str(sales.aggregate(a=Avg('total'))['a'] or 0),
            }
        except Exception:
            pass

        # Top products by sales
        try:
            from django.db.models import Sum, F
            from sales.models import SaleItem
            top = SaleItem.objects.filter(
                sale__status='completed', sale__created_at__date__gte=start_date,
            ).values('product_name').annotate(
                qty=Sum('quantity'), revenue=Sum(F('quantity') * F('unit_price')),
            ).order_by('-revenue')[:5]
            dashboard['top_products'] = [
                {'name': t['product_name'], 'qty': t['qty'], 'revenue': str(t['revenue'])}
                for t in top
            ]
        except Exception:
            pass

        # Stock alerts
        try:
            from django.db.models import F
            from inventory.models import Product
            low_stock = Product.objects.filter(
                is_active=True,
                low_stock_threshold__gt=0,
                stock__lte=F('low_stock_threshold'),
            ).values_list('name', 'stock', 'low_stock_threshold')[:10]
            dashboard['stock_alerts'] = [
                {'name': n, 'stock': s, 'threshold': t}
                for n, s, t in low_stock
            ]
        except Exception:
            pass

        # Recent customers
        try:
            from customers.models import Customer
            recent = Customer.objects.filter(
                is_active=True, created_at__date__gte=start_date,
            ).order_by('-created_at')[:5]
            dashboard['new_customers'] = [
                {'name': c.name, 'date': str(c.created_at.date())}
                for c in recent
            ]
        except Exception:
            pass

        # Pending invoices
        try:
            from django.db.models import Sum
            from invoicing.models import Invoice
            pending = Invoice.objects.filter(status='pending')
            dashboard['pending_invoices'] = {
                'count': pending.count(),
                'total': str(pending.aggregate(t=Sum('total'))['t'] or 0),
            }
        except Exception:
            pass

        # Appointments today
        try:
            from appointments.models import Appointment
            appts = Appointment.objects.filter(
                date=today, status__in=['confirmed', 'pending'],
            )
            dashboard['appointments_today'] = appts.count()
        except Exception:
            pass

        # Employee count
        try:
            from apps.accounts.models import LocalUser
            dashboard['active_employees'] = LocalUser.objects.filter(
                is_active=True, is_deleted=False,
            ).count()
        except Exception:
            pass

        return dashboard


@register_tool
class SearchAcrossModules(AssistantTool):
    name = "search_across_modules"
    description = (
        "Search across products, services, customers, and invoices at once. "
        "Returns grouped results by module."
    )
    short_description = "Search products, services, customers, invoices at once. Grouped by module."
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search text (searches names, SKUs, emails)",
            },
            "limit_per_module": {
                "type": "integer",
                "description": "Max results per module (default: 5)",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    examples = [
        {"query": "María", "limit_per_module": 5},
        {"query": "champú"},
    ]

    def execute(self, args, request):
        query = args['query']
        limit = args.get('limit_per_module', 5)
        results = {}

        # Products
        try:
            from django.db.models import Q
            from inventory.models import Product
            products = Product.objects.filter(
                Q(name__icontains=query) | Q(sku__icontains=query),
                is_active=True,
            )[:limit]
            results['products'] = [
                {'id': str(p.id), 'name': p.name, 'sku': p.sku, 'price': str(p.price), 'stock': p.stock}
                for p in products
            ]
        except Exception:
            pass

        # Services
        try:
            from django.db.models import Q
            from services.models import Service
            services = Service.objects.filter(
                Q(name__icontains=query) | Q(description__icontains=query),
                is_active=True,
            )[:limit]
            results['services'] = [
                {'id': str(s.id), 'name': s.name, 'price': str(s.price), 'duration': s.duration_minutes}
                for s in services
            ]
        except Exception:
            pass

        # Customers
        try:
            from django.db.models import Q
            from customers.models import Customer
            customers = Customer.objects.filter(
                Q(name__icontains=query) | Q(email__icontains=query) | Q(phone__icontains=query),
                is_active=True,
            )[:limit]
            results['customers'] = [
                {'id': str(c.id), 'name': c.name, 'email': c.email, 'phone': c.phone}
                for c in customers
            ]
        except Exception:
            pass

        # Invoices
        try:
            from django.db.models import Q
            from invoicing.models import Invoice
            invoices = Invoice.objects.filter(
                Q(number__icontains=query) | Q(customer_name__icontains=query),
            )[:limit]
            results['invoices'] = [
                {'id': str(i.id), 'number': i.number, 'customer': i.customer_name, 'total': str(i.total), 'status': i.status}
                for i in invoices
            ]
        except Exception:
            pass

        # Sales
        try:
            from sales.models import Sale
            sales = Sale.objects.filter(
                receipt_number__icontains=query,
            )[:limit]
            results['sales'] = [
                {'id': str(s.id), 'receipt': s.receipt_number, 'total': str(s.total), 'status': s.status}
                for s in sales
            ]
        except Exception:
            pass

        total = sum(len(v) for v in results.values())
        return {'query': query, 'total_results': total, 'results': results}


@register_tool
class GetCustomerInsights(AssistantTool):
    name = "get_customer_insights"
    description = (
        "Get detailed customer analysis: purchase history, favorite products/services, "
        "total spent, visit frequency. Search by name or ID."
    )
    short_description = "Get customer analysis: purchases, favorites, total spent, visit frequency."
    parameters = {
        "type": "object",
        "properties": {
            "customer_id": {
                "type": "string",
                "description": "Customer ID (use this if you have it)",
            },
            "customer_name": {
                "type": "string",
                "description": "Customer name to search for (if no ID available)",
            },
        },
        "additionalProperties": False,
    }
    examples = [
        {"customer_id": "123"},
        {"customer_name": "María García"},
    ]

    def execute(self, args, request):
        from customers.models import Customer

        customer = None
        if args.get('customer_id'):
            try:
                customer = Customer.objects.get(id=args['customer_id'])
            except Customer.DoesNotExist:
                return {"error": f"Customer {args['customer_id']} not found"}
        elif args.get('customer_name'):
            customer = Customer.objects.filter(
                name__icontains=args['customer_name'], is_active=True,
            ).first()
            if not customer:
                return {"error": f"No customer found matching '{args['customer_name']}'"}
        else:
            return {"error": "Provide customer_id or customer_name"}

        insights = {
            'customer': {
                'id': str(customer.id),
                'name': customer.name,
                'email': customer.email,
                'phone': customer.phone,
                'lifecycle_stage': getattr(customer, 'lifecycle_stage', ''),
                'created': str(customer.created_at.date()) if hasattr(customer, 'created_at') else '',
            },
        }

        # Purchase history from sales
        try:
            from django.db.models import Sum, Avg
            from sales.models import Sale
            sales = Sale.objects.filter(
                customer_id=customer.id, status='completed',
            )
            insights['purchases'] = {
                'total_orders': sales.count(),
                'total_spent': str(sales.aggregate(t=Sum('total'))['t'] or 0),
                'average_order': str(sales.aggregate(a=Avg('total'))['a'] or 0),
            }

            # Last 5 purchases
            recent = sales.order_by('-created_at')[:5]
            insights['recent_purchases'] = [
                {'date': str(s.created_at.date()), 'total': str(s.total), 'receipt': getattr(s, 'receipt_number', '')}
                for s in recent
            ]
        except Exception:
            pass

        # Top products purchased
        try:
            from django.db.models import Sum
            from sales.models import SaleItem
            top = SaleItem.objects.filter(
                sale__customer_id=customer.id, sale__status='completed',
            ).values('product_name').annotate(
                qty=Sum('quantity'),
            ).order_by('-qty')[:5]
            insights['top_products'] = [
                {'name': t['product_name'], 'quantity': t['qty']}
                for t in top
            ]
        except Exception:
            pass

        # Appointment history
        try:
            from appointments.models import Appointment
            appts = Appointment.objects.filter(customer_id=customer.id)
            insights['appointments'] = {
                'total': appts.count(),
                'upcoming': appts.filter(
                    date__gte=date.today(), status__in=['confirmed', 'pending'],
                ).count(),
            }
        except Exception:
            pass

        # Loyalty data
        try:
            from loyalty.models import LoyaltyMember
            member = LoyaltyMember.objects.filter(customer_id=customer.id).first()
            if member:
                insights['loyalty'] = {
                    'points': member.points,
                    'tier': getattr(member, 'tier', ''),
                }
        except Exception:
            pass

        return insights
