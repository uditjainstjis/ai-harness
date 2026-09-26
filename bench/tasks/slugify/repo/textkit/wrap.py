def truncate_words(text, limit, suffix="..."):
    """Keep at most `limit` words of `text`, appending `suffix` when truncated."""
    words = text.split()
    if len(words) <= limit:
        return text
    return " ".join(words[:limit]) + suffix
