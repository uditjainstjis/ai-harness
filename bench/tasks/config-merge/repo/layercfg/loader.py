from .merge import deep_merge


def load_layers(layers):
    """Combine config layers; later layers win. Returns the merged config."""
    if not layers:
        return {}
    result = layers[0]
    for layer in layers[1:]:
        result = deep_merge(result, layer)
    return result
