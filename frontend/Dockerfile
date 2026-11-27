FROM node:24-alpine

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm ci

COPY . .
RUN npm run build

CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173"]

