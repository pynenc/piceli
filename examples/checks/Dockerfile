# A tiny web server with a login page. `--build-arg LOGIN=no` builds the
# deliberately broken image: it starts and listens (readiness passes) but has
# no /login page, so the release's [[checks]] fail.
FROM docker.io/library/busybox:1.36@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662
ARG LOGIN=yes
RUN mkdir -p /www && if [ "$LOGIN" = yes ]; then echo "Sign in" > /www/login; fi
CMD ["httpd", "-f", "-p", "8080", "-h", "/www"]
