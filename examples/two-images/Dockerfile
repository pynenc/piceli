# One tiny image per MESSAGE: `docker build --build-arg MESSAGE=alpha-v1 .`
FROM docker.io/library/busybox:1.36@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662
ARG MESSAGE
RUN echo "$MESSAGE" > /message
CMD ["sh", "-c", "cat /message && exec sleep 3600"]
