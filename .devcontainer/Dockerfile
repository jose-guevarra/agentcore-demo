FROM public.ecr.aws/amazonlinux/amazonlinux:2023


# Install dependencies
RUN dnf update && dnf install -y \
    git \
    sudo \
    shadow-utils \
    tar \
    vim \
    vi \
    make \
    awscli \
    python3-pip \
    python3.14

RUN dnf clean all

# fails for some reason
#RUN pip3 install uv 

ARG REMOTE_USER=dockeruser
RUN /usr/sbin/groupadd -r sudo \
    && /usr/sbin/useradd -rm -d /home/$REMOTE_USER -s /bin/bash -g sudo -u 1001 $REMOTE_USER \
    && echo '%sudo ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers

USER $REMOTE_USER