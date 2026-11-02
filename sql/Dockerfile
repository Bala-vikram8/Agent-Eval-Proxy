FROM postgres:16
# This image runs psql as postgres; it never starts the server or switches users.
# Remove the unused privilege-switch helper and its vulnerable bundled Go runtime.
RUN rm /usr/local/bin/gosu
# Retain the supported client and its resolved dependencies, not the unused server.
RUN apt-mark manual postgresql-client-16 \
    && DEBIAN_FRONTEND=noninteractive apt-get --yes --auto-remove purge postgresql-16 \
    && rm -rf /var/lib/apt/lists/*
COPY --chown=postgres:postgres init.sql runtime_roles.sql /sql/
VOLUME /tmp
USER postgres
CMD ["bash", "-ec", "psql -v ON_ERROR_STOP=1 -q -f /sql/init.sql && psql -v ON_ERROR_STOP=1 -q -f /sql/runtime_roles.sql"]
