FROM node:22-alpine AS build

WORKDIR /app

# 严格按 lockfile 复现构建；缺 lockfile 即失败（fail-fast）
COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

FROM nginx:alpine

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
