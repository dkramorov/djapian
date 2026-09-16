from django.core.paginator import Paginator


def paginate(queue, per_page):
    try:
        queue = queue.only('id').order_by('id')
    except Exception:
        pass
    paginator = Paginator(queue, per_page)

    for num in paginator.page_range:
        yield paginator.page(num)
