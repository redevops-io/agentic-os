# The egress proxy and the inbound forwarder are standard-library files: no dependencies to patch, nothing else in the image.
FROM python:3.13-slim
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin egress
COPY proxy.py /usr/local/bin/egress-proxy
COPY forward.py /usr/local/bin/egress-forward
USER 10001
EXPOSE 3128
HEALTHCHECK --interval=15s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if b'ok' in urllib.request.urlopen('http://127.0.0.1:3128/healthz', timeout=2).read() else 1)"
ENTRYPOINT ["python", "/usr/local/bin/egress-proxy"]
CMD ["--listen", "0.0.0.0:3128", "--allowlist", "/etc/egress/allowlist.txt"]
