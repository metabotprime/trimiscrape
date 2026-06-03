# Apify-maintained Python + Playwright base image. Pinning the SPECIFIC
# Python + Playwright pair (3.11-1.60.0) for reproducible builds — the
# floating :3.11 tag would silently bump Playwright versions on rebuild
# and could break the JS-side extractors if TikTok's headers shift.
FROM apify/actor-python-playwright:3.11-1.60.0

# Copy dependency manifest first so Docker layer-caches pip install
COPY requirements.txt ./

# Install pinned deps. The base image already has Playwright + browsers
# pre-installed; requirements.txt only adds the Apify SDK.
RUN echo "Python version:" \
 && python --version \
 && echo "Pip version:" \
 && pip --version \
 && pip install --no-cache-dir -r requirements.txt \
 && echo "Apify SDK version:" \
 && python -c "import apify; print(apify.__version__)"

# Copy the source last so iterative edits don't bust the dep cache
COPY . ./

# Default command — Apify SDK wires the rest. src/__main__.py is the entry.
CMD ["python", "-m", "src"]
