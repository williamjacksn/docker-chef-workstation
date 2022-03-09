FROM chef/chefworkstation:21.11.679
# dependabot will find you

FROM python:3.10.0-slim-bullseye

ARG CHEF_WORKSTATION_VERSION="21.11.679"

RUN DEBIAN_FRONTEND=noninteractive \
    /usr/bin/apt-get update \
 && /usr/bin/apt-get --assume-yes install gcc git graphviz libpq-dev make rsync ssh vim-tiny wget \
 && /bin/rm --force --recursive /var/lib/apt/lists/* /tmp/* /var/log/*log /var/log/apt/* /var/lib/dpkg/*-old /var/cache/debconf/*-old

RUN DEBIAN_FRONTEND=noninteractive \
 && /usr/bin/wget --content-disposition "https://packages.chef.io/files/stable/chef-workstation/${CHEF_WORKSTATION_VERSION}/debian/11/chef-workstation_${CHEF_WORKSTATION_VERSION}-1_amd64.deb" --output-document /tmp/chef-workstation.deb \
 && /usr/bin/dpkg --install /tmp/chef-workstation.deb \
 && /usr/bin/apt-get clean \
 && /bin/rm --force --recursive /var/lib/apt/lists/* /tmp/* /var/log/*log /var/log/apt/* /var/lib/dpkg/*-old /var/cache/debconf/*-old

RUN /usr/sbin/adduser --disabled-password --gecos User user

USER user

RUN /usr/local/bin/python -m venv /home/user/venv

COPY --chown=user:user requirements.txt /home/user/docker-chef-workstation/requirements.txt
RUN /home/user/venv/bin/pip install --no-cache-dir --requirement /home/user/docker-chef-workstation/requirements.txt

COPY --chown=user:user scripts /home/user/docker-chef-workstation/scripts

ENV PATH="/home/user/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE="1" \
    PYTHONUNBUFFERED="1" \
    TZ="Etc/UTC"

WORKDIR /home/user/docker-chef-workstation
ENTRYPOINT ["/bin/bash"]

LABEL org.opencontainers.image.authors="William Jackson <william@subtlecoolness.com>" \
      org.opencontainers.image.source="https://github.com/williamjacksn/docker-chef-workstation"
