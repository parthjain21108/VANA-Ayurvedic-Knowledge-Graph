FROM node:22-bookworm-slim AS build

WORKDIR /app

COPY package*.json ./

RUN if [ -f package-lock.json ]; then \
        npm ci; \
    else \
        npm install; \
    fi

COPY . .

ENV VITE_API_BASE_URL=/api

RUN npx vite build


FROM nginx:alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf

COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
